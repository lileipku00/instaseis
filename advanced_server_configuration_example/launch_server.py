#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Launch script for the advanced Instaseis server example.


:copyright:
    Lion Krischer (krischer@geophysik.uni-muenchen.de), 2015
:license:
    GNU Lesser General Public License, Version 3 [non-commercial/academic use]
    (http://www.gnu.org/copyleft/lgpl.html)
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
import argparse
import os

from instaseis.server.app import launch_io_loop

from station_resolver import parse_station_file, get_coordinates

# Simple example assuming a file `station_list.txt` exists in the current
# directory.
FILENAME = "station_list.txt"
conn, cursor = parse_station_file(FILENAME)


def get_station_coordinates(networks, stations):
    return get_coordinates(cursor, networks=networks,
                           stations=stations)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="python -m instaseis.server",
        description='Launch an Instaseis server offering seismograms with a '
                    'REST API.')
    parser.add_argument('--port', type=int, required=True,
                        help='Server port.')
    parser.add_argument('--buffer_size_in_mb', type=int,
                        default=0, help='Size of the buffer in MB')
    parser.add_argument('db_path', type=str,
                        help='Database path')
    parser.add_argument(
        '--quiet', action='store_true',
        help="Don't print any output. Overwrites the 'log_level` setting.")
    parser.add_argument(
        '--log-level', type=str, default='INFO',
        choices=['CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG', 'NOTSET'],
        help='The log level for all Tornado loggers.')

    args = parser.parse_args()
    db_path = os.path.abspath(args.db_path)

    launch_io_loop(db_path=db_path, port=args.port,
                   buffer_size_in_mb=args.buffer_size_in_mb,
                   quiet=args.quiet, log_level=args.log_level,
                   station_coordinates_callback=get_station_coordinates)
