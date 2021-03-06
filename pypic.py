import numpy as np
from scipy.constants import e
import os

from operator import attrgetter, mul

where = os.path.dirname(os.path.abspath(__file__)) + '/'

try:
    from pycuda import driver as cuda
    from pycuda import gpuarray
    from pycuda.compiler import SourceModule
    from pycuda.tools import DeviceData
except ImportError:
    print('pycuda not found. no gpu capabilities will be available')

from gradient.gradient import make_GPU_gradient, numpy_gradient
from m2p.m2p import mesh_to_particles_CPU_2d, mesh_to_particles_CPU_3d
from p2m.p2m import particles_to_mesh_CPU_2d, particles_to_mesh_CPU_3d

# Fortran versions of P2M, M2p
try:
    import rhocompute as rhocom
    import int_field_for as iff
    import int_field_for_border as iffb
except ImportError:
    print('Shared libraries of Fortran versions of m2p/p2m ' +
          '(rhocompute, int_field_for, int_field_for_border) not found. ' +
          'Limited functionality')


def idivup(a, b):
    ''' Compute int(a)//int(b) and round up to next integer if a%b != 0 '''
    a = np.int32(a)
    b = np.int32(b)
    z = (a // b + 1) if (a % b != 0) else (a // b)
    return int(z)


class PyPIC_GPU(object):
    '''Encodes the algorithm of PyPIC for a static mesh
    on the GPU:

    - scatter particles to a fixed mesh which yields
      the charge distribution on the mesh
    - solve the discrete Poisson equation on the mesh
      with the charge distribution to obtain the potential
      on the mesh
    - determine electric fields on the mesh from the potential
    - gather the electric fields back to the particles

    Electrostatics are assumed, magnetic fields are neglected.
    Use the Lorentz transformation to determine the
    electric fields in the beam reference frame and then again
    to transform back to the laboratory reference frame,
    hence accounting for the magnetic fields.
    '''
    def __init__(self, mesh, poissonsolver, context, gradient=make_GPU_gradient):
        '''Mesh sizes need to be powers of 2 in x (and y if it exists).
        '''
        self.mesh = mesh
        self._context = context
        self.poissonsolver = poissonsolver
        if hasattr(poissonsolver, 'is_25D'):
            self.is_25D = True
        self.kernel_call_config = {
                'p2m': {'block': (16, 16, 1),
                        #'grid': (-1, 1, 1) # adapt to number of particles!
                        'grid': (0, 1, 1) # adapt to number of particles!
                        },
                'm2p': {'block': (16, 16, 1),
                        #'grid': (-1, 1, 1) # adapt to number of particles!
                        'grid': (0, 1, 1) # adapt to number of particles!
                        },
                'sorted_p2m': {'block': (256, 1, 1),
                        #'grid': (self.mesh.n_nodes//256, 1, 1)
                        'grid': (idivup(self.mesh.n_nodes, 256), 1, 1)
                        }
                }
        # load kernels
        with open(where + 'p2m/p2m_kernels.cu') as stream:
            source = stream.read()
        p2m_kernels = SourceModule(source)
        with open(where + 'm2p/m2p_kernels.cu') as stream:
            source = stream.read()
        m2p_kernels = SourceModule(source)

        self._gradient = gradient(mesh, context)

        # initialize in init because otherwise it tries to compile even if
        # no instance of the class is created -> errors if you import the module
        # without having a running pycuda context
        # depending on the dimension, the correct funtions are loaded
        self._particles_to_mesh_kernel = (
            p2m_kernels.get_function('particles_to_mesh_' +
                                     str(mesh.dimension) + 'd'))
        self._sorted_particles_to_guard_mesh_kernel = (
            p2m_kernels.get_function('cic_guard_cell_weights_' +
                                     str(mesh.dimension) + 'd'))
        self._join_guard_cells_kernel = (
            p2m_kernels.get_function('join_guard_cells_' +
                                     str(mesh.dimension) + 'd'))
        self._mesh_to_particles_kernel = (
            m2p_kernels.get_function('mesh_to_particles_' +
                                     str(mesh.dimension) + 'd'))
        self._field_to_particles_kernel = (
            m2p_kernels.get_function('field_to_particles_' +
                                     str(mesh.dimension) + 'd'))

        # prepare calls to kernels!!!
        self._particles_to_mesh_kernel.prepare(
                'i' + 'P' + 'i'*(mesh.dimension-1) + 'P'*2**mesh.dimension +
                'P'*mesh.dimension)
        self._field_to_particles_kernel.prepare(
                'i' + 'PP' + 'i'*(mesh.dimension-1) + 'P'*2**mesh.dimension +
                'P'*mesh.dimension)
        self._mesh_to_particles_kernel.prepare(
                'i' + 'P'*mesh.dimension*2 + 'i'*(mesh.dimension-1) +
                'P'*2**mesh.dimension + 'P'*mesh.dimension)
        self._sorted_particles_to_guard_mesh_kernel.prepare(
                'P'*mesh.dimension + 'd'*2*mesh.dimension +
                'i'*(mesh.dimension-1) + 'i' + 'PP' + 'P'*2**mesh.dimension)
        self._join_guard_cells_kernel.prepare('P'*2**mesh.dimension
                + 'i' + 'i'*mesh.dimension + 'P')



    def particles_to_mesh(self, *mp_coords, **kwargs):
        '''Scatter the macro-particles onto the mesh nodes.
        The argument list mp_coords defines the coordinate arrays of
        the macro-particles, e.g. in 3D
            mp_coords = (x, y, z)
        The keyword argument charge=e is the charge per macro-particle.
        Further possible keyword arguments are
        mesh_indices=None, mesh_distances=None, mesh_weights=None .

        Return the charge distribution on the mesh (which is mesh_charges =
        rho*volume).
        '''
        mesh_indices = kwargs.get("mesh_indices",
                                  self.mesh.get_indices(*mp_coords))
        mesh_weights = kwargs.get(
            "mesh_weights", self.mesh.get_weights(
                *mp_coords, indices=mesh_indices,
                distances=kwargs.get("mesh_distances", None)
            )
        )
        charge = kwargs.get("charge", e)
        n_macroparticles = len(mp_coords[0])
        self.kernel_call_config['p2m']['grid'] = (
                idivup(n_macroparticles, reduce(mul,
                           self.kernel_call_config['p2m']['block'],1))
                , 1, 1
            )
        block = self.kernel_call_config['p2m']['block']
        grid = self.kernel_call_config['p2m']['grid']
        mesh_count = gpuarray.zeros(shape=self.mesh.shape, #self.mesh.n_nodes,
                                    dtype=np.float64)
        args = [np.int32(n_macroparticles)] + [mesh_count]
        args += self.mesh.shape_r[:-1] + mesh_weights + mesh_indices
        self._particles_to_mesh_kernel(
            *args,
            block=block,
            grid=grid
         )
        self._context.synchronize()
        mesh_charges = mesh_count*charge
        return mesh_charges

    def sorted_particles_to_mesh(self, *mp_coords, **kwargs):
        '''Scatter the macro-particles onto the mesh nodes.
        Assumes the macro-particles to be sorted by mesh node id.

        The argument list mp_coords defines the coordinate arrays of
        the macro-particles, e.g. in 3D
            mp_coords = (x, y, z)

        The two mandatory keyword arguments lower_bounds and upper_bounds
        are index arrays. They indicate the start and end indices
        within the sorted particle arrays for each node id.
        The respective node id is identical to the index within
        lower_bounds and upper_bounds.

        The keyword argument charge=e is the charge per macro-particle.

        Return the charge distribution on the mesh (which is mesh_charges =
        rho*volume).
        '''
        lower_bounds = kwargs['lower_bounds']
        upper_bounds = kwargs['upper_bounds']
        charge = kwargs.get("charge", e)

        guard_charge_pointers = [
            gpuarray.empty(self.mesh.n_nodes, dtype=np.float64).gpudata
            for _ in xrange(2**self.mesh.dimension)
        ]
        block = self.kernel_call_config['sorted_p2m']['block']
        grid = self.kernel_call_config['sorted_p2m']['grid']
        self._sorted_particles_to_guard_mesh_kernel.prepared_call(*(
            [grid, block,] +
            # particles
            map(attrgetter('gpudata'), mp_coords) +
            # mesh
            list(self.mesh.origin) +
            list(self.mesh.distances) +
            list(self.mesh.shape_r[:-1]) + [self.mesh.n_nodes] +
            [lower_bounds.gpudata, upper_bounds.gpudata] +
            # guard cells
            guard_charge_pointers
        ))
        mesh_charges = gpuarray.zeros(self.mesh.shape, dtype=np.float64)
        self._context.synchronize()
        self._join_guard_cells_kernel.prepared_call(*(
            [grid, block,] +
            guard_charge_pointers +
            [self.mesh.n_nodes] + list(self.mesh.shape_r) +
            [mesh_charges.gpudata]
        ))
        self._context.synchronize()
        mesh_charges *= e
        return mesh_charges

        # # example on how to use the sorted one with PyHEADTAIL:
        # idx = gpuarray.zeros(n_particles, dtype=np.int32)
        # mod.get_sort_perm_int(mesh.get_node_ids(beam.x, beam.y, beam.z), idx)
        # beam.reorder(idx)
        # node_ids = mesh.get_node_ids(beam.x, beam.y, beam.z)
        # lower_bounds = gpuarray.empty(mesh.n_nodes, dtype=np.int32)
        # upper_bounds = gpuarray.empty(mesh.n_nodes, dtype=np.int32)
        # seq = gpuarray.arange(mesh.n_nodes, dtype=np.int32)
        # mod.lower_bound_int(node_ids, seq, lower_bounds)
        # mod.upper_bound_int(node_ids, seq, upper_bounds)

        # mesh_charges = pypicalg.particles_to_mesh(beam.x, beam.y, beam.z)
        # context.synchronize()

    def poisson_solve(self, mesh_charges):
        '''Solve the discrete Poisson equation with the charge
        distribution rho on the mesh, -divgrad phi = rho / epsilon_0 .
        mesh_charges  =rho*volume

        Return the potential phi.
        '''
        # does self._context.synchronize() within solve
        return self.poissonsolver.poisson_solve(mesh_charges)

    def poisson_cholsolve(self, rho):
        '''test only'''
        return self.poissonsolver.poisson_cholsolve(rho)

    def get_electric_fields(self, phi):
        '''Return electric fields on the mesh given
        the potential phi on the mesh via
        E = - grad phi .

        Returns asynchronously from the device.
        (You may potentially want to call context.synchronize()!)
        '''
        grad = self._gradient(-phi)
        grad = [g.reshape(self.mesh.shape) for g in grad]
        return grad

    def mesh_to_particles(self, mesh_quantity, *mp_coords, **kwargs):
        '''Interpolate the mesh_quantity (whose shape is the mesh shape)
        onto the particles. The argument list mp_coords defines the
        coordinate arrays of the macro-particles, e.g. in 3D
            mp_coords = (x, y, z)
        Possible keyword arguments are
        mesh_indices=None, mesh_distances=None, mesh_weights=None .

        Return the interpolated quantity in an array for each particle.

        Returns asynchronously from the device.
        (You may potentially want to call context.synchronize()!)
        '''
        mesh_indices = kwargs.get("mesh_indices",
                                  self.mesh.get_indices(*mp_coords))
        mesh_weights = kwargs.get(
            "mesh_weights", self.mesh.get_weights(
                *mp_coords, indices=mesh_indices,
                distances=kwargs.get("mesh_distances", None)
            )
        )
        n_macroparticles = len(mp_coords[0])
        particles_quantity = gpuarray.empty(n_macroparticles, dtype=np.float64)

        self.kernel_call_config['m2p']['grid'] = (
                idivup(n_macroparticles, reduce(mul,
                    self.kernel_call_config['m2p']['block'],1))
                , 1, 1
            )
        block = self.kernel_call_config['m2p']['block']
        grid = self.kernel_call_config['m2p']['grid']

        self._mesh_to_particles_kernel(
            np.int32(n_macroparticles),
            particles_quantity, mesh_quantity,
            *(self.mesh.shape[:-1] + mesh_weights + mesh_indices),
            block=block, grid=grid
        )
        return particles_quantity

    def field_to_particles(self, *mesh_fields_and_mp_coords, **kwargs):
        '''Gather the three-dimensional (electric) field
        from the mesh to the particles.
        The list mesh_fields_and_mp_coords consists of 2-tuples for each
        dimension where
        - each first entry is the field array on the mesh,
        - each second entry is the particle coordinate array,
        e.g. in 3D
            mesh_fields_and_mp_coords = ((E_x, x), (E_y, y), (E_z, z))
        where E_x, E_y, E_z would be given by self.get_electric_fields
        and x, y, z are the particle coordinate arrays.
        Possible keyword arguments are
        mesh_indices=None, mesh_distances=None, mesh_weights=None .

        Return the interpolated fields per particle for each dimension.
        '''
        mesh_fields, mp_coords = zip(*mesh_fields_and_mp_coords)
        mesh_indices = kwargs.get("mesh_indices",
                                  self.mesh.get_indices(*mp_coords))
        mesh_weights = kwargs.get(
            "mesh_weights", self.mesh.get_weights(
                *mp_coords, indices=mesh_indices,
                distances=kwargs.get("mesh_distances", None)
            )
        )
        n_macroparticles = len(mp_coords[0])
        self.kernel_call_config['m2p']['grid'] = (
                idivup(n_macroparticles, reduce(mul,
                    self.kernel_call_config['m2p']['block'],1))
                 , 1, 1
        )
        # field per particle
        particle_fields = [gpuarray.empty(shape=n_macroparticles,
                                          dtype=np.float64)
                           for _ in mesh_fields]
        block = self.kernel_call_config['m2p']['block']
        grid = self.kernel_call_config['m2p']['grid']
        args = [np.int32(n_macroparticles)] + particle_fields + list(mesh_fields)
        args += list(self.mesh.shape_r[:-1]) #strides
        args += list(mesh_weights)
        args += list(mesh_indices)
        # interpolate to particles on gpu.
        # interpolation only, multiply with charge afterwards
        self._field_to_particles_kernel(
            *args, block=block, grid=grid
        )
        return particle_fields

    def pic_solve(self, *mp_coords, **kwargs):
        '''Encapsulates the whole algorithm to determine the
        fields of the particles on themselves.
        The keyword argument charge=e is the charge per macro-particle.
        Further keyword arguments are
        mesh_indices=None, mesh_distances=None, mesh_weights=None .

        The optional keyword arguments lower_bounds=False and
        upper_bounds=False trigger the use of sorted_particles_to_mesh
        which assumes the particles to be sorted by the node ids of the
        mesh. (see further info there.)
        This results in particle deposition to be 3.5x quicker and
        mesh to particle interpolation to be 0.25x quicker.
        (Timing for 1e6 particles and a 64x64x32 mesh includes sorting.)

        Return as many interpolated fields per particle as
        dimensions in mp_coords are given.
        '''
        charge = kwargs.get("charge", e)
        mesh_indices = kwargs.get("mesh_indices",
                                  self.mesh.get_indices(*mp_coords))
        mesh_weights = kwargs.get(
            "mesh_weights", self.mesh.get_weights(
                *mp_coords, indices=mesh_indices,
                distances=kwargs.get("mesh_distances", None)
            )
        )

        lower_bounds = kwargs.get('lower_bounds', False)
        upper_bounds = kwargs.get('upper_bounds', False)

        if lower_bounds and upper_bounds:
            mesh_charges = self.sorted_particles_to_mesh(
                *mp_coords, charge=charge,
                lower_bounds=lower_bounds, upper_bounds=upper_bounds
            )
        else: # particle arrays are not sorted by mesh node ids
            mesh_charges = self.particles_to_mesh(
                *mp_coords, charge=charge,
                mesh_indices=mesh_indices,
                mesh_weights=mesh_weights
            )
        rho = mesh_charges / self.mesh.volume_elem
        if self.is_25D:
            rho *= self.mesh.dz
        phi = self.poisson_solve(rho)
        mesh_e_fields = self.get_electric_fields(phi)
        self._context.synchronize()
        mesh_fields_and_mp_coords = zip(list(mesh_e_fields), list(mp_coords))
        fields = self.field_to_particles(*mesh_fields_and_mp_coords,
                                         mesh_indices=mesh_indices,
                                         mesh_weights=mesh_weights)
        self._context.synchronize()
        return fields

    # PyPIC backwards compatibility
    scatter = particles_to_mesh
    gather = field_to_particles



class PyPIC(object):
    '''Encodes the algorithm of PyPIC for a static mesh
    on the CPU:

    - scatter particles to a fixed mesh which yields
      the charge distribution on the mesh
    - solve the discrete Poisson equation on the mesh
      with the charge distribution to obtain the potential
      on the mesh
    - determine electric fields on the mesh from the potential
    - gather the electric fields back to the particles

    Electrostatics are assumed, magnetic fields are neglected.
    Use the Lorentz transformation to determine the
    electric fields in the beam reference frame and then again
    to transform back to the laboratory reference frame,
    hence accounting for the magnetic fields.
    '''

    def __init__(self, mesh, poissonsolver, gradient=numpy_gradient):
        self.mesh = mesh
        self.poissonsolver = poissonsolver
        self._gradient = gradient(mesh)
        if mesh.dimension == 2:
            self._p2m_kernel = particles_to_mesh_CPU_2d
            self._m2p_kernel = mesh_to_particles_CPU_2d
        elif mesh.dimension == 3:
            self._p2m_kernel = particles_to_mesh_CPU_3d
            self._m2p_kernel = mesh_to_particles_CPU_3d
        else:
            raise RuntimeError("Only meshes with dim=2,3 are supported yet")


    def particles_to_mesh(self, *mp_coords, **kwargs):
        '''Scatter the macro-particles onto the mesh nodes.
        The argument list mp_coords defines the coordinate arrays of
        the macro-particles, e.g. in 3D
            mp_coords = (x, y, z)
        The keyword argument charge=e is the charge per macro-particle.
        Further keyword arguments are
        mesh_indices=None, mesh_distances=None, mesh_weights=None .

        Return the charge distribution on the mesh.
        '''
        mesh_indices = kwargs.get("mesh_indices",
                                  self.mesh.get_indices(*mp_coords))
        mesh_weights = kwargs.get(
            "mesh_weights", self.mesh.get_weights(
                *mp_coords, indices=mesh_indices,
                distances=kwargs.get("mesh_distances", None)
            )
        )
        charge = kwargs.get("charge", e)
        n_macroparticles = len(mp_coords[0])
        mesh_density = self._p2m_kernel(self.mesh, n_macroparticles,
                                        mesh_indices, mesh_weights)
        rho = mesh_density*charge
        return rho

    def poisson_solve(self, rho):
        '''Solve the discrete Poisson equation with the charge
        distribution rho on the mesh, -divgrad phi = rho / epsilon_0 .

        Return the potential phi.
        '''
        # does self._context.synchronize() within solve
        return self.poissonsolver.poisson_solve(rho)

    def get_electric_fields(self, phi):
        '''Return electric fields on the mesh given
        the potential phi on the mesh via
        E = - grad phi .
        '''
        return self._gradient(phi)

    def mesh_to_particles(self, mesh_quantity, *mp_coords, **kwargs):
        '''Interpolate the mesh_quantity (whose shape is the mesh shape)
        onto the particles. The argument list mp_coords defines the
        coordinate arrays of the macro-particles, e.g. in 3D
            mp_coords = (x, y, z)
        Possible keyword arguments are
        mesh_indices=None, mesh_distances=None, mesh_weights=None .

        Return the interpolated quantity in an array for each particle.

        Returns asynchronously from the device.
        (You may potentially want to call context.synchronize()!)
        '''
        mesh_indices = kwargs.get("mesh_indices",
                                  self.mesh.get_indices(*mp_coords))
        mesh_weights = kwargs.get(
            "mesh_weights", self.mesh.get_weights(
                *mp_coords, indices=mesh_indices,
                distances=kwargs.get("mesh_distances", None)
            )
        )
        n_macroparticles = len(mp_coords[0])
        particles_quantity = np.empty(n_macroparticles, dtype=np.float64)
        particles_quantity = self._m2p_kernel(self.mesh, mesh_quantity,
                                             mesh_indices, mesh_weights)
        return particles_quantity

    def field_to_particles(self, *mesh_fields_and_mp_coords, **kwargs):
        '''Gather the three-dimensional (electric) field
        from the mesh to the particles.
        The list mesh_fields_and_mp_coords consists of 2-tuples for each
        dimension where
        - each first entry is the field array on the mesh,
        - each second entry is the particle coordinate array,
        e.g. in 3D
            mesh_fields_and_mp_coords = ((E_x, x), (E_y, y), (E_z, z))
        where E_x, E_y, E_z would be given by self.get_electric_fields
        and x, y, z are the particle coordinate arrays.
        Possible keyword arguments are
        mesh_indices=None, mesh_distances=None, mesh_weights=None .

        Return the interpolated fields per particle for each dimension.
        '''
        mesh_fields, mp_coords = zip(*mesh_fields_and_mp_coords)
        mesh_indices = kwargs.get("mesh_indices",
                                  self.mesh.get_indices(*mp_coords))
        mesh_weights = kwargs.get(
            "mesh_weights", self.mesh.get_weights(
                *mp_coords, indices=mesh_indices,
                distances=kwargs.get("mesh_distances", None)
            )
        )
        n_macroparticles = len(mp_coords[0])

        # field per particle
        particle_fields = [np.empty(shape=n_macroparticles,
                                    dtype=np.float64)
                           for _ in mesh_fields]
        for idx, field in enumerate(mesh_fields):
            #call mesh_to_particles once per dimension
            particle_fields[idx] = self.mesh_to_particles(field, *mp_coords,
                                        mesh_indices=mesh_indices,
                                        mesh_weights=mesh_weights)

        return particle_fields

    def pic_solve(self, *mp_coords, **kwargs):
        '''Encapsulates the whole algorithm to determine the
        fields of the particles on themselves.
        The keyword argument charge=e is the charge per macro-particle.
        Further keyword arguments are
        mesh_indices=None, mesh_distances=None, mesh_weights=None .

        Return as many interpolated fields per particle as
        dimensions in mp_coords are given.
        '''
        mesh_indices = kwargs.get("mesh_indices",
                                  self.mesh.get_indices(*mp_coords))
        mesh_weights = kwargs.get(
            "mesh_weights", self.mesh.get_weights(
                *mp_coords, indices=mesh_indices,
                distances=kwargs.get("mesh_distances", None)
            )
        )
        charge = kwargs.get("charge", e)

        mesh_charges = self.particles_to_mesh(*mp_coords, charge=charge,
                                     mesh_indices=mesh_indices,
                                     mesh_weights=mesh_weights)
        rho = 1./self.mesh.volume_elem * mesh_charges
        phi = self.poisson_solve(rho)
        mesh_e_fields = self.get_electric_fields(phi)
        for i, field in enumerate(mesh_e_fields):
            mesh_e_fields[i] = field.flatten()
        mesh_fields_and_mp_coords = zip(list(mesh_e_fields), list(mp_coords))
        fields = self.field_to_particles(*mesh_fields_and_mp_coords,
                                         mesh_indices=mesh_indices,
                                         mesh_weights=mesh_weights)
        return fields


class PyPIC_Fortran_M2P_P2M(PyPIC):
    ''' Uses the fast M2P/P2M Fortran routines
    2D only!
    Provide backwards compatibility and access to the fast Fortran M2P/P2M
    If the poissonsolver has an 'flag_border_mat' attribute, the
    int_field_for_border function is used instead of the int_field function.
    '''

    def __init__(self, mesh, poissonsolver, gradient=numpy_gradient):
        super(PyPIC_Fortran_M2P_P2M, self).__init__(mesh, poissonsolver,
                gradient)
        self.mesh = mesh
        self.poissonsolver = poissonsolver
        self._gradient = gradient(mesh)


    def field_to_particles(self, *mesh_fields_and_mp_coords, **kwargs):
        [ex, ey], [x, y] = zip(*mesh_fields_and_mp_coords)
        ex = ex.reshape((self.mesh.ny, self.mesh.nx)).T
        ey = ey.reshape((self.mesh.ny, self.mesh.nx)).T
        if hasattr(self.poissonsolver, 'flag_inside_n_mat'):
            flag_inside_n_mat = self.poissonsolver.flag_inside_n_mat
            Ex, Ey = iffb.int_field_border(x, y, self.mesh.x0, self.mesh.y0,
                                   self.mesh.dx, self.mesh.dx, ex, ey,
                                   flag_inside_n_mat)
        else:
            if hasattr(self.poissonsolver, 'flag_border_mat'):
                #Only for Staircase_SquareGrid solver
                ex[self.poissonsolver.flag_border_mat] *= 2
                ey[self.poissonsolver.flag_border_mat] *= 2
            Ex, Ey = iff.int_field(x, y, self.mesh.x0, self.mesh.y0,
                                   self.mesh.dx, self.mesh.dx, ex, ey)
        return [Ex, Ey]

    def particles_to_mesh(self, *mp_coords, **kwargs):
        x, y = mp_coords #only 2 dimensions are supported
        charge = kwargs.get("charge", e)
        nel_mp = charge * np.ones(x.shape)
        rho = rhocom.compute_sc_rho(x, y, nel_mp, self.mesh.x0, self.mesh.y0,
                                    self.mesh.dx, self.mesh.nx, self.mesh.ny)
        return rho.reshape(self.mesh.nx, self.mesh.ny).T
