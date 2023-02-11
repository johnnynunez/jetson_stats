# -*- coding: UTF-8 -*-
# This file is part of the jetson_stats package (https://github.com/rbonghi/jetson_stats or http://rnext.it).
# Copyright (c) 2019-2023 Raffaello Bonghi.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import os
import re
import stat
import shlex
# Logging
import logging
import subprocess as sp
from .engine import read_engine
from .common import cat
from .command import Command
# Create logger
logger = logging.getLogger(__name__)
# Memory regular exception
MEMINFO_REG = re.compile(r'(?P<key>.+):\s+(?P<value>.+) (?P<unit>.)B')
BUDDYINFO_REG = re.compile(r'Node\s+(?P<numa_node>\d+).*zone\s+(?P<zone>\w+)\s+(?P<nr_free>.*)')
MEM_TABLE_REG = re.compile(r'(?P<user>\w+)\s+(?P<process>[^ ]+)\s+(?P<PID>\d+)\s+(?P<size>\d+)(?P<unit>\w)\n')
TOT_TABLE_REG = re.compile(r'total\s+(?P<size>\d+)(?P<unit>\w)')
SWAP_REG = re.compile(r'(?P<name>[^ ]+)\s+(?P<type>[^ ]+)\s+(?P<size>\d+)\s+(?P<used>\d+)\s+(?P<prio>-?\d+)')
# Swap configuration
PATH_FSTAB = '/etc/fstab'
CONFIG_DEFAULT_SWAP_DIRECTORY = ''
CONFIG_DEFAULT_SWAP_NAME = 'swfile'


def meminfo():
    # Read meminfo and decode
    # https://access.redhat.com/solutions/406773
    status_mem = {}
    with open("/proc/meminfo", 'r') as fp:
        for line in fp:
            # Search line
            match = re.search(MEMINFO_REG, line.strip())
            if match:
                parsed_line = match.groupdict()
                status_mem[parsed_line['key']] = {'val': int(parsed_line['value']), 'unit': parsed_line['unit']}
    return status_mem


def buddyinfo(page_size):
    # Read status free memory
    # http://andorian.blogspot.com/2014/03/making-sense-of-procbuddyinfo.html
    buddyhash = {}
    with open("/proc/buddyinfo", 'r') as fp:
        buddyinfo = fp.readlines()
    for line in buddyinfo:
        # Decode line
        parsed_line = re.match(BUDDYINFO_REG, line.strip()).groupdict()
        # detect buddy size
        numa_node = int(parsed_line["numa_node"])
        free_fragments = [int(i) for i in parsed_line["nr_free"].split()]
        max_order = len(free_fragments)
        fragment_sizes = [page_size * 2**order for order in range(0, max_order)]
        usage_in_bytes = [free * fragmented for free, fragmented in zip(free_fragments, fragment_sizes)]
        data = {
            "zone": parsed_line["zone"],
            "nr_free": free_fragments,
            "sz_fragment": fragment_sizes,
            "usage": usage_in_bytes}
        buddyhash[numa_node] = buddyhash[numa_node] + [data] if numa_node in buddyhash else [data]
    return buddyhash


def read_mem_table(path_table):
    """
    This method list all processes working with GPU

    ========== ============ ======== =============
    user       process      PID      size
    ========== ============ ======== =============
    user       name process number   dictionary
    ========== ============ ======== =============

    :return: list of all processes
    :type spin: list
    """
    table = []
    total = {}
    with open(path_table, "r") as fp:
        for line in fp:
            # Search line
            match = re.search(MEM_TABLE_REG, line)
            if match:
                parsed_line = match.groupdict()
                data = [
                    parsed_line['user'],
                    parsed_line['process'],
                    parsed_line['PID'],
                    {'size': int(parsed_line['size']), 'unit': parsed_line['unit'].lower()}
                ]
                table += [data]
                continue
            # Find total on table
            match = re.search(TOT_TABLE_REG, line)
            if match:
                parsed_line = match.groupdict()
                total = {'size': int(parsed_line['size']), 'unit': parsed_line['unit'].lower()}
                continue
    # return total and table
    return total, table


def read_swapon():
    """
    This method list all processes working with GPU

    ============== ======================= ======== =============
    name           type                    prio     size
    ============== ======================= ======== =============
    name partition type: partition or file priority dictionary
    ============== ======================= ======== =============

    :return: list of all processes
    :type spin: list
    """
    table = {}
    swap = Command(['swapon', '--show', '--raw', '--byte'])
    lines = swap()
    for line in lines:
        # Search line
        match = re.search(SWAP_REG, line.strip())
        if match:
            parsed_line = match.groupdict()
            data = {
                'type': parsed_line['type'],
                'prio': int(parsed_line['prio']),
                'size': int(parsed_line['size']) // 1024,
                'used': int(parsed_line['used']) // 1024,
                'unit': 'k',
            }
            table[parsed_line['name']] = data
    return table


def check_fstab(table_line):
    with open(PATH_FSTAB, "r") as fp:
        for line in fp:
            if table_line == line.strip():
                return True
    return False


def read_emc():
    emc = {}
    if os.path.isdir("/sys/kernel/debug/bpmp/debug/clk/emc"):
        path = "/sys/kernel/debug/bpmp/debug/clk/emc"
        # Add unit
        emc['unit'] = 'k'
        # Check if access to this file
        if os.access(path + "/rate", os.R_OK):
            with open(path + "/rate", 'r') as f:
                # Write min
                emc['cur'] = int(f.read()) // 1000
        # Check if access to this file
        if os.access(path + "/max_rate", os.R_OK):
            with open(path + "/max_rate", 'r') as f:
                # Write min
                emc['max'] = int(f.read()) // 1000
        # Check if access to this file
        if os.access(path + "/min_rate", os.R_OK):
            with open(path + "/min_rate", 'r') as f:
                # Write min
                emc['min'] = int(f.read()) // 1000
        # Check if access to this file
        if os.access(path + "/mrq_rate_locked", os.R_OK):
            with open(path + "/mrq_rate_locked", 'r') as f:
                # Write min
                emc['override'] = int(f.read()) // 1000
    elif os.path.isdir("/sys/kernel/debug/tegra_bwmgr"):
        path = "/sys/kernel/debug/clk/override.emc"
        # Add unit
        emc['unit'] = 'k'
        # Check if access to this file
        if os.access(path + "/clk_rate", os.R_OK):
            with open(path + "/clk_rate", 'r') as f:
                # Write min
                emc['cur'] = int(f.read()) // 1000
        # Check if access to this file
        if os.access(path + "/clk_state", os.R_OK):
            with open(path + "/clk_state", 'r') as f:
                # Write min
                emc['override'] = int(f.read()) // 1000
        # Decode from tegra_bwmgr
        path = "/sys/kernel/debug/tegra_bwmgr"
        # Check if access to this file
        if os.access(path + "/emc_max_rate", os.R_OK):
            with open(path + "/emc_max_rate", 'r') as f:
                # Write min
                emc['max'] = int(f.read()) // 1000
        # Check if access to this file
        if os.access(path + "/emc_min_rate", os.R_OK):
            with open(path + "/emc_min_rate", 'r') as f:
                # Write min
                emc['min'] = int(f.read()) // 1000
    elif os.path.isdir("/sys/kernel/debug/clk/emc"):
        emc = read_engine("/sys/kernel/debug/clk/emc")
    # Fix max frequency
    emc_cap = 0
    # Check if access to this file
    if os.access("/sys/kernel/nvpmodel_emc_cap/emc_iso_cap", os.R_OK):
        with open("/sys/kernel/nvpmodel_emc_cap/emc_iso_cap", 'r') as f:
            # Write min
            emc_cap = int(f.read()) // 1000
    # Fix max EMC
    if 'max' in emc:
        if emc_cap > 0 and emc_cap < emc['max']:
            emc['max'] = emc_cap
    return emc


class Memory(object):
    """
    This class get the output from your memory, this class is readable like a dictionary,
    please read the documentation on :py:attr:`~jtop.jtop.memory` but is also usable to enable, disable swap on your device

    .. code-block:: python

        with jtop() as jetson:
            if jetson.ok():
                jetson.memory.swap_set(10, on_boot=False)


    or if you want to deactivate a swap you can use this command

    .. code-block:: python

        with jtop() as jetson:
            if jetson.ok():
                jetson.memory.swap_deactivate()

    Below all methods available using the :py:attr:`~jtop.jtop.memory` attribute
    """

    def __init__(self):
        self._controller = None
        self._data = {}
        self._swap_path = ''

    def swap_path(self):
        """
        Return the default SWAP path

        :return: Path swap
        :rtype: str
        """
        return self._swap_path

    def clear_cache(self):
        """
        Clear the memory cache
        """
        # Set new swap size configuration
        self._controller.put({'clear_cache': ''})

    def swap_is_enable(self, path):
        """
        Check if a swap is on list

        :param path: Path swap
        :type path: str

        :return: Status swap
        :rtype: bool
        """
        return path in self._data['SWAP']['table']

    def swap_set(self, value, path='', on_boot=False):
        """
        Create a new swap on a default path `/`

        :param value: Size in **G** of a new SWAP
        :type value: int
        :param path: Path swap
        :type path: str
        :param on_boot: Set this swap on boot
        :type on_boot: bool
        :raises ValueError: Wrong speed number or wrong mode name
        """
        if not isinstance(value, (int, float)):
            raise ValueError("Need a Number")
        # if path_swap is empty load from default configuration
        if not path:
            path = self._swap_path
        # Set new swap size configuration
        self._controller.put({'swap': {'type': 'set', 'path': path, 'size': value, 'boot': on_boot}})

    def swap_deactivate(self, path=''):
        """
        Deactivate a swap from a path or from default location `/`

        :param path: Path swap
        :type path: str
        """
        # if path_swap is empty load from default configuration
        if not path:
            path = self._swap_path
        # Set new swap size configuration
        self._controller.put({'swap': {'type': 'unset', 'path': path}})

    def _initialize(self, controller, path):
        self._controller = controller
        self._swap_path = path

    def _update(self, data):
        self._data = data

    def items(self):
        return self._data.items()

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __next__(self):
        return next(self._data)


class MemoryService(object):

    def __init__(self, config):
        self._config = config
        # Extract memory page size
        self._page_size = os.sysconf("SC_PAGE_SIZE")
        # board type
        self._isJetson = os.path.isfile("/sys/kernel/debug/nvmap/iovmm/maps")
        self._is_emc = True if read_emc() else False
        if not self._is_emc:
            logger.warn("EMC not available")

    def swap_path(self):
        config = self._config.get('swap', {})
        directory = config.get('directory', CONFIG_DEFAULT_SWAP_DIRECTORY)
        swap_name = config.get('name', CONFIG_DEFAULT_SWAP_NAME)
        return "{directory}/{name}".format(directory=directory, name=swap_name)

    def clear_cache(self):
        """
        Clear cache following https://coderwall.com/p/ef1gcw/managing-ram-and-swap
        """
        clear_cache = Command(['sysctl', 'vm.drop_caches=3'])
        out = clear_cache()
        return True if out else False

    @staticmethod
    def swap_set(size, path_swap, on_boot):
        if os.path.isfile(path_swap):
            logger.error("{path_swap} already exist".format(path_swap=path_swap))
            return
        # Load swap configuration
        logger.info("Activate {path_swap} auto={on_boot}".format(path_swap=path_swap, on_boot=on_boot))
        # Create a swapfile for Ubuntu at the current directory location
        sp.call(shlex.split('fallocate -l {size}G {path_swap}'.format(size=size, path_swap=path_swap)))
        # Change permissions so that only root can use it
        # https://www.tutorialspoint.com/python/os_chmod.htm
        # Equivalent permission 600 srw-------
        os.chmod(path_swap, stat.S_IREAD | stat.S_IWRITE)
        # Set up the Linux swap area
        sp.call(shlex.split('mkswap {path_swap}'.format(path_swap=path_swap)))
        # Now start using the swapfile
        sp.call(shlex.split('swapon {path_swap}'.format(path_swap=path_swap)))
        # Add not on boot return
        if not on_boot:
            return
        # Find if is already on boot
        swap_string_boot = "{path_swap} none swap sw 0 0".format(path_swap=path_swap)
        if check_fstab(swap_string_boot):
            logger.warn("{path_swap} Already on boot".format(path_swap=path_swap))
            return
        # Append swap line
        file_object = open(PATH_FSTAB, 'a')
        file_object.write("{swap_string_boot}\n".format(swap_string_boot=swap_string_boot))
        file_object.close()

    @staticmethod
    def swap_deactivate(path_swap):
        # Check if exist swap
        if not os.path.isfile(path_swap):
            logger.error("{path_swap} Does not exist".format(path_swap=path_swap))
            return
        # Disable swap
        sp.call(shlex.split('swapoff {path_swap}'.format(path_swap=path_swap)))
        # Remove swap
        os.remove(path_swap)
        # Remove if on fstab
        swap_string_boot = "{path_swap} none swap sw 0 0".format(path_swap=path_swap)
        if not check_fstab(swap_string_boot):
            return
        # Check if is on boot
        logger.info("Remove {path_swap} from fstab".format(path_swap=path_swap))
        with open(PATH_FSTAB, "r") as f:
            lines = f.readlines()
        with open(PATH_FSTAB, "w") as f:
            for line in lines:
                if line.strip("\n") != swap_string_boot:
                    f.write(line)
        # Run script
        logger.info("Deactivate {path_swap}".format(path_swap=path_swap))

    def get_status(self):
        memory = {}
        # Measure the largest free bank for 4MB
        mem_size = buddyinfo(self._page_size)
        # Count only the biggest Large free bank (lfb)
        large_free_bank = 0
        for _, data in mem_size.items():
            large_free_bank += sum([zone['nr_free'][-1] for zone in data])
        # Status Memory
        status_mem = meminfo()
        # Read memory use
        # NvMapMemUsed: Is the shared memory between CPU and GPU
        # This key is always available on Jetson (not really always)
        ram_shared = status_mem.get('NvMapMemUsed', {})
        ram_shared_val = ram_shared.get('size', 0)
        table = []
        if self._isJetson:
            # Update table
            # Use the memory table to measure
            total, table = read_mem_table("/sys/kernel/debug/nvmap/iovmm/maps")
            # Update shared size
            ram_shared_val = total['size'] if ram_shared_val == 0 else ram_shared_val
        # Extract memory info
        ram_total = status_mem.get('MemTotal', {})
        ram_free = status_mem.get('MemFree', {})
        # ram_available = status_mem.get('MemAvailable', {})
        ram_buffer = status_mem.get('Buffers', {})
        ram_cached = status_mem.get('Cached', {})
        ram_SReclaimable = status_mem.get('SReclaimable', {})
        ram_Shmem = status_mem.get('Shmem', {})
        total_used_memory = ram_total.get('val', 0) - ram_free.get('val', 0)
        cached_memory = ram_cached.get('val', 0) + ram_SReclaimable.get('val', 0)  # + ram_Shmem.get('val', 0)
        # Add fields for RAM
        memory['RAM'] = {
            'tot': ram_total.get('val', 0),
            'used': total_used_memory - (ram_buffer.get('val', 0) + cached_memory),
            'free': ram_free.get('val', 0),
            'buffers': ram_buffer.get('val', 0),
            'cached': cached_memory + ram_Shmem.get('val', 0),
            'shared': ram_shared_val,
            'unit': ram_total.get('unit', 'k'),
            'lfb': large_free_bank,  # In 4MB
        }
        # Add memory table ONLY if available
        if table:
            memory['RAM']['table'] = table
        # Extract swap numbers
        swap_total = status_mem.get('SwapTotal', {})
        swap_free = status_mem.get('SwapFree', {})
        swap_cached = status_mem.get('SwapCached', {})
        # Add fields for swap
        memory['SWAP'] = {
            'tot': swap_total.get('val', 0),
            'used': swap_total.get('val', 0) - swap_free.get('val', 0),
            'cached': swap_cached.get('val', 0),
            'unit': swap_total.get('unit', 'k'),
            'table': read_swapon(),
        }
        # Read EMC status
        if self._is_emc:
            memory['EMC'] = read_emc()
            # Set always online this engine
            memory['EMC']['online'] = True
            # Percentage utilization
            # https://forums.developer.nvidia.com/t/real-time-emc-bandwidth-with-sysfs/107479/3
            utilization = int(cat("/sys/kernel/actmon_avg_activity/mc_all"))
            memory['EMC']['val'] = utilization // memory['EMC']['cur']
        # Read IRAM if available
        if os.path.isdir("/sys/kernel/debug/nvmap/iram"):
            size = 0
            if os.path.isfile("/sys/kernel/debug/nvmap/iram/size"):
                # Convert from Hex to decimal - Number in bytes
                size = int(cat("/sys/kernel/debug/nvmap/iram/size"), 16) // 1024
            used_total, _ = read_mem_table("/sys/kernel/debug/nvmap/iram/clients")
            memory['IRAM'] = {
                'tot': size,
                'used': used_total.get('val', 0),
                'unit': used_total.get('unit', 'k'),
                'lfb': size - used_total.get('val', 0),  # TODO To check
            }
        return memory
# EOF
