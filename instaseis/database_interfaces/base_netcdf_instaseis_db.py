#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Python library to extract seismograms from a set of wavefields generated by
AxiSEM.

:copyright:
    Martin van Driel (Martin@vanDriel.de), 2014
    Lion Krischer (krischer@geophysik.uni-muenchen.de), 2014
:license:
    GNU Lesser General Public License, Version 3 [non-commercial/academic use]
    (http://www.gnu.org/copyleft/lgpl.html)
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

from future.utils import with_metaclass

from abc import ABCMeta, abstractmethod
import collections

import numpy as np
from obspy.signal.util import next_pow_2
import os

from .base_instaseis_db import BaseInstaseisDB
from .. import finite_elem_mapping
from .. import helpers
from .. import rotations
from .. import sem_derivatives
from .. import spectral_basis


ElementInfo = collections.namedtuple("ElementInfo", [
    "id_elem", "gll_point_ids", "xi", "eta", "corner_points", "col_points_xi",
    "col_points_eta", "axis", "eltype"])

Coordinates = collections.namedtuple("Coordinates", ["s", "phi", "z"])


class BaseNetCDFInstaseisDB(with_metaclass(ABCMeta, BaseInstaseisDB)):
    """
    Base class for extracting seismograms from a local Instaseis netCDF
    database.
    """
    def __init__(self, db_path, buffer_size_in_mb=100,
                 read_on_demand=False, *args, **kwargs):
        """
        :param db_path: Path to the Instaseis Database containing
            subdirectories PZ and/or PX each containing a
            ``order_output.nc4`` file.
        :type db_path: str
        :param buffer_size_in_mb: Strain and displacement are buffered to
            avoid repeated disc access. Depending on the type of database
            and the number of components of the database, the total buffer
            memory can be up to four times this number. The optimal value is
            highly application and system dependent.
        :type buffer_size_in_mb: int, optional
        :param read_on_demand: Read several global fields on demand (faster
            initialization) or on initialization (slower
            initialization, faster in individual seismogram extraction,
            useful e.g. for finite sources, default).
        :type read_on_demand: bool, optional
        """
        self.db_path = db_path
        self.buffer_size_in_mb = buffer_size_in_mb
        self.read_on_demand = read_on_demand

    def _get_element_info(self, coordinates):
        """
        Find and collect/calculate information about the element containing
        the given coordinates.
        """
        k_map = {"displ_only": 6,
                 "strain_only": 1,
                 "fullfields": 1}

        nextpoints = self.parsed_mesh.kdtree.query(
            [coordinates.s, coordinates.z], k=k_map[self.info.dump_type])

        # Find the element containing the point of interest.
        mesh = self.parsed_mesh.f["Mesh"]
        if self.info.dump_type == 'displ_only':
            for idx in nextpoints[1]:
                corner_points = np.empty((4, 2), dtype="float64")

                if not self.read_on_demand:
                    corner_point_ids = self.parsed_mesh.fem_mesh[idx][:4]
                    eltype = self.parsed_mesh.eltypes[idx]
                    corner_points[:, 0] = \
                        self.parsed_mesh.mesh_S[corner_point_ids]
                    corner_points[:, 1] = \
                        self.parsed_mesh.mesh_Z[corner_point_ids]
                else:
                    corner_point_ids = mesh["fem_mesh"][idx][:4]

                    # When reading from a netcdf file, the indices must be
                    # sorted for newer netcdf versions. The double argsort()
                    # gives the indices in the sorted array to restore the
                    # original order.
                    eltype = mesh["eltype"][idx]

                    m_s = mesh["mesh_S"]
                    m_z = mesh["mesh_Z"]
                    corner_points[:, 0] = [m_s[_i] for _i in corner_point_ids]
                    corner_points[:, 1] = [m_z[_i] for _i in corner_point_ids]

                isin, xi, eta = finite_elem_mapping.inside_element(
                    coordinates.s, coordinates.z, corner_points, eltype,
                    tolerance=1E-3)
                if isin:
                    id_elem = idx
                    break
            else:  # pragma: no cover
                raise ValueError("Element not found")

            if not self.read_on_demand:
                gll_point_ids = self.parsed_mesh.sem_mesh[id_elem]
                axis = bool(self.parsed_mesh.axis[id_elem])
            else:
                gll_point_ids = mesh["sem_mesh"][id_elem]
                axis = bool(mesh["axis"][id_elem])

            if axis:
                col_points_xi = self.parsed_mesh.glj_points
                col_points_eta = self.parsed_mesh.gll_points
            else:
                col_points_xi = self.parsed_mesh.gll_points
                col_points_eta = self.parsed_mesh.gll_points
        else:
            id_elem = nextpoints[1]
            col_points_xi = None
            col_points_eta = None
            gll_point_ids = None
            axis = None
            corner_points = None
            eltype = None
            xi = None
            eta = None

        return ElementInfo(
            id_elem=id_elem, gll_point_ids=gll_point_ids, xi=xi, eta=eta,
            corner_points=corner_points, col_points_xi=col_points_xi,
            col_points_eta=col_points_eta, axis=axis, eltype=eltype)

    @abstractmethod
    def _get_data(self, source, receiver, components, coordinates,
                  element_info):
        """
        Has to be implemented by each implementation.

        Must return a dictionary with the keys being the components, and the
        values the corresponding data arrays.

        :param source: The source.
        :param receiver: The receiver.
        :param components: The requested components.
        :param coordinates: The coordinates in correct coordinates system.
        :param element_info: Information about the element containing the
            coordinates.
        """
        raise NotImplementedError

    def _get_seismograms(self, source, receiver, components=("Z", "N", "E")):
        """
        Extract seismograms from a netCDF based Instaseis database.

        :type source: :class:`instaseis.source.Source` or
            :class:`instaseis.source.ForceSource`
        :param source: The source.
        :type receiver: :class:`instaseis.source.Receiver`
        :param receiver: The receiver.
        :type components: tuple
        :param components: The requests components. Any combinations of
            ``"Z"``, ``"N"``, ``"E"``, ``"R"``, and ``"T"``
        """
        if self.info.is_reciprocal:
            a, b = source, receiver
        else:
            a, b = receiver, source

        rotmesh_s, rotmesh_phi, rotmesh_z = rotations.rotate_frame_rd(
            a.x(planet_radius=self.info.planet_radius),
            a.y(planet_radius=self.info.planet_radius),
            a.z(planet_radius=self.info.planet_radius),
            b.longitude, b.colatitude)

        coordinates = Coordinates(s=rotmesh_s, phi=rotmesh_phi, z=rotmesh_z)

        element_info = self._get_element_info(coordinates=coordinates)

        return self._get_data(
            source=source, receiver=receiver, components=components,
            coordinates=coordinates, element_info=element_info)

    def _get_strain_interp(self, mesh, id_elem, gll_point_ids, G, GT,
                           col_points_xi, col_points_eta, corner_points,
                           eltype, axis, xi, eta):
        if id_elem not in mesh.strain_buffer:
            # Single precision in the NetCDF files but the later interpolation
            # routines require double precision. Assignment to this array will
            # force a cast.
            utemp = np.zeros((mesh.ndumps, mesh.npol + 1, mesh.npol + 1, 3),
                             dtype=np.float64, order="F")

            # The list of ids we have is unique but not sorted.
            ids = gll_point_ids.flatten()
            s_ids = np.sort(ids)
            mesh_dict = mesh.f["Snapshots"]

            # Load displacement from all GLL points.
            for i, var in enumerate(["disp_s", "disp_p", "disp_z"]):
                if var not in mesh_dict:
                    continue

                # Make sure it can work with normal and transposed arrays to
                # support legacy as well as modern, transposed databases.
                time_axis = mesh.time_axis[var]

                # Chunk the I/O by requesting successive indices in one go -
                # this actually makes quite a big difference on some file
                # systems.
                chunks = helpers.io_chunker(s_ids)
                _temp = []
                m = mesh_dict[var]
                if time_axis == 0:
                    for _c in chunks:
                        if isinstance(_c, list):
                            _temp.append(m[:, _c[0]:_c[1]])
                        else:
                            _temp.append(m[:, _c])
                else:
                    for _c in chunks:
                        if isinstance(_c, list):
                            _temp.append(m[_c[0]:_c[1], :].T)
                        else:
                            _temp.append(m[_c, :].T)

                _t = np.empty((_temp[0].shape[0], 25),
                              dtype=_temp[0].dtype)

                k = 0
                for _i in _temp:
                    if len(_i.shape) == 1:
                        _t[:, k] = _i
                        k += 1
                    else:
                        for _j in range(_i.shape[1]):
                            _t[:, k + _j] = _i[:, _j]

                        k += _j + 1

                _temp = _t

                for ipol in range(mesh.npol + 1):
                    for jpol in range(mesh.npol + 1):
                        idx = ipol * 5 + jpol
                        utemp[:, jpol, ipol, i] = \
                            _temp[:, np.argwhere(
                                s_ids == ids[idx])[0][0]]

            strain_fct_map = {
                "monopole": sem_derivatives.strain_monopole_td,
                "dipole": sem_derivatives.strain_dipole_td,
                "quadpole": sem_derivatives.strain_quadpole_td}

            strain = strain_fct_map[mesh.excitation_type](
                utemp, G, GT, col_points_xi, col_points_eta, mesh.npol,
                mesh.ndumps, corner_points, eltype, axis)

            mesh.strain_buffer.add(id_elem, strain)
        else:
            strain = mesh.strain_buffer.get(id_elem)

        final_strain = np.empty((strain.shape[0], 6), order="F")

        for i in range(6):
            final_strain[:, i] = spectral_basis.lagrange_interpol_2D_td(
                col_points_xi, col_points_eta, strain[:, :, :, i], xi, eta)

        if not mesh.excitation_type == "monopole":
            final_strain[:, 3] *= -1.0
            final_strain[:, 5] *= -1.0

        return final_strain

    def _get_strain(self, mesh, id_elem):
        if id_elem not in mesh.strain_buffer:
            strain_temp = np.zeros((self.info.npts, 6), order="F")

            mesh_dict = mesh.f["Snapshots"]

            for i, var in enumerate([
                    'strain_dsus', 'strain_dsuz', 'strain_dpup',
                    'strain_dsup', 'strain_dzup', 'straintrace']):
                if var not in mesh_dict:
                    continue
                strain_temp[:, i] = mesh_dict[var][:, id_elem]

            # transform strain to voigt mapping
            # dsus, dpup, dzuz, dzup, dsuz, dsup
            final_strain = np.empty((self.info.npts, 6), order="F")
            final_strain[:, 0] = strain_temp[:, 0]
            final_strain[:, 1] = strain_temp[:, 2]
            final_strain[:, 2] = (strain_temp[:, 5] - strain_temp[:, 0] -
                                  strain_temp[:, 2])
            final_strain[:, 3] = -strain_temp[:, 4]
            final_strain[:, 4] = strain_temp[:, 1]
            final_strain[:, 5] = -strain_temp[:, 3]
            mesh.strain_buffer.add(id_elem, final_strain)
        else:
            final_strain = mesh.strain_buffer.get(id_elem)

        return final_strain

    def _get_displacement(self, mesh, id_elem, gll_point_ids, col_points_xi,
                          col_points_eta, xi, eta):
        if id_elem not in mesh.displ_buffer:
            utemp = np.zeros((mesh.ndumps, mesh.npol + 1, mesh.npol + 1, 3),
                             dtype=np.float64, order="F")

            mesh_dict = mesh.f["Snapshots"]

            # Load displacement from all GLL points.
            for i, var in enumerate(["disp_s", "disp_p", "disp_z"]):
                if var not in mesh_dict:
                    continue
                # The netCDF Python wrappers starting with version 1.1.6
                # disallow duplicate and unordered indices while slicing. So
                # we need to do it manually.
                # The list of ids we have is unique but not sorted.
                ids = gll_point_ids.flatten()
                s_ids = np.sort(ids)
                temp = mesh_dict[var][:, s_ids]
                for ipol in range(mesh.npol + 1):
                    for jpol in range(mesh.npol + 1):
                        idx = ipol * 5 + jpol
                        utemp[:, jpol, ipol, i] = \
                            temp[:, np.argwhere(s_ids == ids[idx])[0][0]]

            mesh.displ_buffer.add(id_elem, utemp)
        else:
            utemp = mesh.displ_buffer.get(id_elem)

        final_displacement = np.empty((utemp.shape[0], 3), order="F")

        for i in range(3):
            final_displacement[:, i] = spectral_basis.lagrange_interpol_2D_td(
                col_points_xi, col_points_eta, utemp[:, :, :, i], xi, eta)

        return final_displacement

    def _get_info(self):
        """
        Returns a dictionary with information about the currently loaded
        database.
        """
        # Get the size of all netCDF files.
        filesize = 0
        for m in self.meshes:
            if m:
                filesize += os.path.getsize(m.filename)

        if self._is_reciprocal:
            if self.meshes.pz is not None and self.meshes.px is not None:
                components = 'vertical and horizontal'
            elif self.meshes.pz is None and self.meshes.px is not None:
                components = 'horizontal only'
            elif self.meshes.pz is not None and self.meshes.px is None:
                components = 'vertical only'
        else:
            components = '4 elemental moment tensors'

        return dict(
            is_reciprocal=self._is_reciprocal,
            components=components,
            source_depth=float(self.parsed_mesh.source_depth)
            if self._is_reciprocal is False else None,
            velocity_model=self.parsed_mesh.background_model,
            external_model_name=self.parsed_mesh.external_model_name,
            attenuation=self.parsed_mesh.attenuation,
            period=float(self.parsed_mesh.dominant_period),
            dump_type=self.parsed_mesh.dump_type,
            excitation_type=self.parsed_mesh.excitation_type,
            dt=float(self.parsed_mesh.dt),
            sampling_rate=float(1.0 / self.parsed_mesh.dt),
            npts=int(self.parsed_mesh.ndumps),
            nfft=int(next_pow_2(self.parsed_mesh.ndumps) * 2),
            length=float(self.parsed_mesh.dt * (self.parsed_mesh.ndumps - 1)),
            stf=self.parsed_mesh.stf_kind,
            src_shift=float(self.parsed_mesh.source_shift),
            src_shift_samples=int(self.parsed_mesh.source_shift_samp),
            slip=self.parsed_mesh.stf_norm,
            sliprate=self.parsed_mesh.stf_d_norm,
            spatial_order=int(self.parsed_mesh.npol),
            min_radius=float(self.parsed_mesh.kwf_rmin) * 1e3,
            max_radius=float(self.parsed_mesh.kwf_rmax) * 1e3,
            planet_radius=float(self.parsed_mesh.planet_radius),
            min_d=float(self.parsed_mesh.kwf_colatmin),
            max_d=float(self.parsed_mesh.kwf_colatmax),
            time_scheme=self.parsed_mesh.time_scheme,
            directory=os.path.relpath(self.db_path),
            filesize=filesize,
            compiler=self.parsed_mesh.axisem_compiler,
            user=self.parsed_mesh.axisem_user,
            format_version=int(self.parsed_mesh.file_version),
            axisem_version=self.parsed_mesh.axisem_version,
            datetime=self.parsed_mesh.creation_time
        )
