#
#

# Copyright (C) 2006, 2007, 2008, 2009, 2010, 2011, 2012, 2013, 2014 Google Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
# IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
# TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


"""Configuration management for Ganeti

This module provides the interface to the Ganeti cluster configuration.

The configuration data is stored on every node but is updated on the master
only. After each update, the master distributes the data to the other nodes.

Currently, the data storage format is JSON. YAML was slow and consuming too
much memory.

"""

# pylint: disable=R0904
# R0904: Too many public methods

import copy
import os
import random
import logging
import time
import threading
import itertools

from ganeti.config.temporary_reservations import TemporaryReservationManager

from ganeti import errors
from ganeti import utils
from ganeti import constants
import ganeti.wconfd as wc
from ganeti import objects
from ganeti import serializer
from ganeti import uidpool
from ganeti import netutils
from ganeti import runtime
from ganeti import pathutils
from ganeti import network


def GetWConfdContext(ec_id, livelock):
  """Prepare a context for communication with WConfd.

  WConfd needs to know the identity of each caller to properly manage locks and
  detect job death. This helper function prepares the identity object given a
  job ID (optional) and a livelock file.

  @type ec_id: int, or None
  @param ec_id: the job ID or None, if the caller isn't a job
  @type livelock: L{ganeti.utils.livelock.LiveLock}
  @param livelock: a livelock object holding the lockfile needed for WConfd
  @return: the WConfd context

  """
  if ec_id is None:
    return (threading.current_thread().getName(),
            livelock.GetPath(), os.getpid())
  else:
    return (ec_id,
            livelock.GetPath(), os.getpid())


def GetConfig(ec_id, livelock, **kwargs):
  """A utility function for constructing instances of ConfigWriter.

  It prepares a WConfd context and uses it to create a ConfigWriter instance.

  @type ec_id: int, or None
  @param ec_id: the job ID or None, if the caller isn't a job
  @type livelock: L{ganeti.utils.livelock.LiveLock}
  @param livelock: a livelock object holding the lockfile needed for WConfd
  @type kwargs: dict
  @param kwargs: Any additional arguments for the ConfigWriter constructor
  @rtype: L{ConfigWriter}
  @return: the ConfigWriter context

  """
  kwargs['wconfdcontext'] = GetWConfdContext(ec_id, livelock)
  kwargs['wconfd'] = wc.Client()
  return ConfigWriter(**kwargs)


def _ConfigSync(shared=0):
  """Configuration synchronization decorator.

  """
  def wrap(fn):
    def sync_function(*args, **kwargs):
      with args[0].GetConfigManager(shared):
        return fn(*args, **kwargs)
    return sync_function
  return wrap

# job id used for resource management at config upgrade time
_UPGRADE_CONFIG_JID = "jid-cfg-upgrade"


def _ValidateConfig(data):
  """Verifies that a configuration dict looks valid.

  This only verifies the version of the configuration.

  @raise errors.ConfigurationError: if the version differs from what
      we expect

  """
  if data['version'] != constants.CONFIG_VERSION:
    raise errors.ConfigVersionMismatch(constants.CONFIG_VERSION,
                                       data['version'])


def _MatchNameComponentIgnoreCase(short_name, names):
  """Wrapper around L{utils.text.MatchNameComponent}.

  """
  return utils.MatchNameComponent(short_name, names, case_sensitive=False)


def _CheckInstanceDiskIvNames(disks):
  """Checks if instance's disks' C{iv_name} attributes are in order.

  @type disks: list of L{objects.Disk}
  @param disks: List of disks
  @rtype: list of tuples; (int, string, string)
  @return: List of wrongly named disks, each tuple contains disk index,
    expected and actual name

  """
  result = []

  for (idx, disk) in enumerate(disks):
    exp_iv_name = "disk/%s" % idx
    if disk.iv_name != exp_iv_name:
      result.append((idx, exp_iv_name, disk.iv_name))

  return result


class ConfigManager(object):
  """Locks the configuration and exposes it to be read or modified.

  """
  def __init__(self, config_writer, shared=False, forcelock=False):
    assert isinstance(config_writer, ConfigWriter), \
           "invalid argument: Not a ConfigWriter"
    self._config_writer = config_writer
    self._shared = shared
    self._forcelock = forcelock

  def __enter__(self):
    try:
      self._config_writer._OpenConfig(# pylint: disable=W0212
                                      self._shared,
                                      force=self._forcelock)
    except Exception:
      logging.debug("Opening configuration failed")
      try:
        self._config_writer._CloseConfig(False) # pylint: disable=W0212
      except Exception: # pylint: disable=W0703
        logging.debug("Closing configuration failed as well")
      raise

  def __exit__(self, exc_type, exc_value, traceback):
    # save the configuration, if this was a write opreration that succeeded
    if exc_type is not None:
      logging.debug("Configuration operation failed,"
                    " the changes will not be saved")
    # pylint: disable=W0212
    self._config_writer._CloseConfig(not self._shared and exc_type is None)
    return False


def _UpdateIvNames(base_idx, disks):
  """Update the C{iv_name} attribute of disks.

  @type disks: list of L{objects.Disk}

  """
  for (idx, disk) in enumerate(disks):
    disk.iv_name = "disk/%s" % (base_idx + idx)


class ConfigWriter(object):
  """The interface to the cluster configuration.

  WARNING: The class is no longer thread-safe!
  Each thread must construct a separate instance.

  @ivar _all_rms: a list of all temporary reservation managers

  """
  def __init__(self, cfg_file=None, offline=False, _getents=runtime.GetEnts,
               accept_foreign=False, wconfdcontext=None, wconfd=None):
    self.write_count = 0
    self._config_data = None
    self._SetConfigData(None)
    self._offline = offline
    if cfg_file is None:
      self._cfg_file = pathutils.CLUSTER_CONF_FILE
    else:
      self._cfg_file = cfg_file
    self._getents = _getents
    self._temporary_ids = TemporaryReservationManager()
    self._all_rms = [self._temporary_ids]
    # Note: in order to prevent errors when resolving our name later,
    # we compute it here once and reuse it; it's
    # better to raise an error before starting to modify the config
    # file than after it was modified
    self._my_hostname = netutils.Hostname.GetSysName()
    self._cfg_id = None
    self._wconfdcontext = wconfdcontext
    self._wconfd = wconfd
    self._accept_foreign = accept_foreign
    self._lock_count = 0
    self._lock_current_shared = None
    self._lock_forced = False

  def _ConfigData(self):
    return self._config_data

  def OutDate(self):
    self._config_data = None

  def _SetConfigData(self, cfg):
    self._config_data = cfg

  def _GetWConfdContext(self):
    return self._wconfdcontext

  # this method needs to be static, so that we can call it on the class
  @staticmethod
  def IsCluster():
    """Check if the cluster is configured.

    """
    return os.path.exists(pathutils.CLUSTER_CONF_FILE)

  @_ConfigSync(shared=1)
  def GetNdParams(self, node):
    """Get the node params populated with cluster defaults.

    @type node: L{objects.Node}
    @param node: The node we want to know the params for
    @return: A dict with the filled in node params

    """
    nodegroup = self._UnlockedGetNodeGroup(node.group)
    return self._ConfigData().cluster.FillND(node, nodegroup)

  @_ConfigSync(shared=1)
  def GetNdGroupParams(self, nodegroup):
    """Get the node groups params populated with cluster defaults.

    @type nodegroup: L{objects.NodeGroup}
    @param nodegroup: The node group we want to know the params for
    @return: A dict with the filled in node group params

    """
    return self._UnlockedGetNdGroupParams(nodegroup)

  def _UnlockedGetNdGroupParams(self, group):
    """Get the ndparams of the group.

    @type group: L{objects.NodeGroup}
    @param group: The group we want to know the params for
    @rtype: dict of str to int
    @return: A dict with the filled in node group params

    """
    return self._ConfigData().cluster.FillNDGroup(group)

  @_ConfigSync(shared=1)
  def GetGroupSshPorts(self):
    """Get a map of group UUIDs to SSH ports.

    @rtype: dict of str to int
    @return: a dict mapping the UUIDs to the SSH ports

    """
    port_map = {}
    for uuid, group in self._config_data.nodegroups.items():
      ndparams = self._UnlockedGetNdGroupParams(group)
      port = ndparams.get(constants.ND_SSH_PORT)
      port_map[uuid] = port
    return port_map

  @_ConfigSync(shared=1)
  def GetInstanceDiskParams(self, instance):
    """Get the disk params populated with inherit chain.

    @type instance: L{objects.Instance}
    @param instance: The instance we want to know the params for
    @return: A dict with the filled in disk params

    """
    node = self._UnlockedGetNodeInfo(instance.primary_node)
    nodegroup = self._UnlockedGetNodeGroup(node.group)
    return self._UnlockedGetGroupDiskParams(nodegroup)

  @_ConfigSync()
  def SetInstanceDiskTemplate(self, inst_uuid, disk_template):
    """Set the instance's disk template to the given value.

    @type inst_uuid: string
    @param inst_uuid: The UUID of the instance object
    @type disk_template: string
    @param disk_template: The new disk template of the instance

    """
    instance = self._UnlockedGetInstanceInfo(inst_uuid)
    if instance is None:
      raise errors.ConfigurationError("Unknown instance '%s'" % inst_uuid)

    # Update the disk template of the instance
    instance.disk_template = disk_template

  def _UnlockedGetInstanceDisks(self, inst_uuid):
    """Return the disks' info for the given instance

    @type inst_uuid: string
    @param inst_uuid: The UUID of the instance we want to know the disks for

    @rtype: List of L{objects.Disk}
    @return: A list with all the disks' info

    """
    instance = self._UnlockedGetInstanceInfo(inst_uuid)
    if instance is None:
      raise errors.ConfigurationError("Unknown instance '%s'" % inst_uuid)

    return [self._UnlockedGetDiskInfo(disk_uuid)
            for disk_uuid in instance.disks]

  @_ConfigSync(shared=1)
  def GetInstanceDisks(self, inst_uuid):
    """Return the disks' info for the given instance

    This is a simple wrapper over L{_UnlockedGetInstanceDisks}.

    """
    return self._UnlockedGetInstanceDisks(inst_uuid)

  def _UnlockedAddDisk(self, disk):
    """Add a disk to the config.

    @type disk: L{objects.Disk}
    @param disk: The disk object

    """
    if not isinstance(disk, objects.Disk):
      raise errors.ProgrammerError("Invalid type passed to _UnlockedAddDisk")

    logging.info("Adding disk %s to configuration", disk.uuid)

    self._CheckUniqueUUID(disk, include_temporary=False)
    disk.serial_no = 1
    disk.ctime = disk.mtime = time.time()
    disk.UpgradeConfig()
    self._ConfigData().disks[disk.uuid] = disk
    self._ConfigData().cluster.serial_no += 1

  def _UnlockedAttachInstanceDisk(self, inst_uuid, disk_uuid, idx=None):
    """Attach a disk to an instance.

    @type inst_uuid: string
    @param inst_uuid: The UUID of the instance object
    @type disk_uuid: string
    @param disk_uuid: The UUID of the disk object
    @type idx: int
    @param idx: the index of the newly attached disk; if not
      passed, the disk will be attached as the last one.

    """
    instance = self._UnlockedGetInstanceInfo(inst_uuid)
    if instance is None:
      raise errors.ConfigurationError("Instance %s doesn't exist"
                                      % inst_uuid)
    if disk_uuid not in self._ConfigData().disks:
      raise errors.ConfigurationError("Disk %s doesn't exist" % disk_uuid)

    if idx is None:
      idx = len(instance.disks)
    else:
      if idx < 0:
        raise IndexError("Not accepting negative indices other than -1")
      elif idx > len(instance.disks):
        raise IndexError("Got disk index %s, but there are only %s" %
                         (idx, len(instance.disks)))

    # Disk must not be attached anywhere else
    for inst in self._ConfigData().instances.values():
      if disk_uuid in inst.disks:
        raise errors.ReservationError("Disk %s already attached to instance %s"
                                      % (disk_uuid, inst.name))

    instance.disks.insert(idx, disk_uuid)
    instance_disks = self._UnlockedGetInstanceDisks(inst_uuid)
    _UpdateIvNames(idx, instance_disks[idx:])
    instance.serial_no += 1
    instance.mtime = time.time()

  @_ConfigSync()
  def AddInstanceDisk(self, inst_uuid, disk, idx=None):
    """Add a disk to the config and attach it to instance.

    This is a simple wrapper over L{_UnlockedAddDisk} and
    L{_UnlockedAttachInstanceDisk}.

    """
    self._UnlockedAddDisk(disk)
    self._UnlockedAttachInstanceDisk(inst_uuid, disk.uuid, idx)

  def _UnlockedDetachInstanceDisk(self, inst_uuid, disk_uuid):
    """Detach a disk from an instance.

    @type inst_uuid: string
    @param inst_uuid: The UUID of the instance object
    @type disk_uuid: string
    @param disk_uuid: The UUID of the disk object

    """
    instance = self._UnlockedGetInstanceInfo(inst_uuid)
    if instance is None:
      raise errors.ConfigurationError("Instance %s doesn't exist"
                                      % inst_uuid)
    if disk_uuid not in self._ConfigData().disks:
      raise errors.ConfigurationError("Disk %s doesn't exist" % disk_uuid)

    # Check if disk is attached to the instance
    if disk_uuid not in instance.disks:
      raise errors.ProgrammerError("Disk %s is not attached to an instance"
                                   % disk_uuid)

    idx = instance.disks.index(disk_uuid)
    instance.disks.remove(disk_uuid)
    instance_disks = self._UnlockedGetInstanceDisks(inst_uuid)
    _UpdateIvNames(idx, instance_disks[idx:])
    instance.serial_no += 1
    instance.mtime = time.time()

  def _UnlockedRemoveDisk(self, disk_uuid):
    """Remove the disk from the configuration.

    @type disk_uuid: string
    @param disk_uuid: The UUID of the disk object

    """
    if disk_uuid not in self._ConfigData().disks:
      raise errors.ConfigurationError("Disk %s doesn't exist" % disk_uuid)

    # Disk must not be attached anywhere
    for inst in self._ConfigData().instances.values():
      if disk_uuid in inst.disks:
        raise errors.ReservationError("Cannot remove disk %s. Disk is"
                                      " attached to instance %s"
                                      % (disk_uuid, inst.name))

    # Remove disk from config file
    del self._ConfigData().disks[disk_uuid]
    self._ConfigData().cluster.serial_no += 1

  @_ConfigSync()
  def RemoveInstanceDisk(self, inst_uuid, disk_uuid):
    """Detach a disk from an instance and remove it from the config.

    This is a simple wrapper over L{_UnlockedDetachInstanceDisk} and
    L{_UnlockedRemoveDisk}.

    """
    self._UnlockedDetachInstanceDisk(inst_uuid, disk_uuid)
    self._UnlockedRemoveDisk(disk_uuid)

  def _UnlockedGetDiskInfo(self, disk_uuid):
    """Returns information about a disk.

    It takes the information from the configuration file.

    @param disk_uuid: UUID of the disk

    @rtype: L{objects.Disk}
    @return: the disk object

    """
    if disk_uuid not in self._ConfigData().disks:
      return None

    return self._ConfigData().disks[disk_uuid]

  @_ConfigSync(shared=1)
  def GetDiskInfo(self, disk_uuid):
    """Returns information about a disk.

    This is a simple wrapper over L{_UnlockedGetDiskInfo}.

    """
    return self._UnlockedGetDiskInfo(disk_uuid)

  def _AllInstanceNodes(self, inst_uuid):
    """Compute the set of all disk-related nodes for an instance.

    This abstracts away some work from '_UnlockedGetInstanceNodes'
    and '_UnlockedGetInstanceSecondaryNodes'.

    @type inst_uuid: string
    @param inst_uuid: The UUID of the instance we want to get nodes for
    @rtype: set of strings
    @return: A set of names for all the nodes of the instance

    """
    instance = self._UnlockedGetInstanceInfo(inst_uuid)
    if instance is None:
      raise errors.ConfigurationError("Unknown instance '%s'" % inst_uuid)

    instance_disks = self._UnlockedGetInstanceDisks(inst_uuid)
    all_nodes = []
    for disk in instance_disks:
      all_nodes.extend(disk.all_nodes)
    return (set(all_nodes), instance)

  def _UnlockedGetInstanceNodes(self, inst_uuid):
    """Get all disk-related nodes for an instance.

    For non-DRBD, this will be empty, for DRBD it will contain both
    the primary and the secondaries.

    @type inst_uuid: string
    @param inst_uuid: The UUID of the instance we want to get nodes for
    @rtype: list of strings
    @return: A list of names for all the nodes of the instance

    """
    (all_nodes, instance) = self._AllInstanceNodes(inst_uuid)
    # ensure that primary node is always the first
    all_nodes.discard(instance.primary_node)
    return (instance.primary_node, ) + tuple(all_nodes)

  @_ConfigSync(shared=1)
  def GetInstanceNodes(self, inst_uuid):
    """Get all disk-related nodes for an instance.

    This is just a wrapper over L{_UnlockedGetInstanceNodes}

    """
    return self._UnlockedGetInstanceNodes(inst_uuid)

  def _UnlockedGetInstanceSecondaryNodes(self, inst_uuid):
    """Get the list of secondary nodes.

    @type inst_uuid: string
    @param inst_uuid: The UUID of the instance we want to get nodes for
    @rtype: list of strings
    @return: A list of names for all the secondary nodes of the instance

    """
    (all_nodes, instance) = self._AllInstanceNodes(inst_uuid)
    all_nodes.discard(instance.primary_node)
    return tuple(all_nodes)

  @_ConfigSync(shared=1)
  def GetInstanceSecondaryNodes(self, inst_uuid):
    """Get the list of secondary nodes.

    This is a simple wrapper over L{_UnlockedGetInstanceSecondaryNodes}.

    """
    return self._UnlockedGetInstanceSecondaryNodes(inst_uuid)

  def _UnlockedGetInstanceLVsByNode(self, inst_uuid, lvmap=None):
    """Provide a mapping of node to LVs a given instance owns.

    @type inst_uuid: string
    @param inst_uuid: The UUID of the instance we want to
        compute the LVsByNode for
    @type lvmap: dict
    @param lvmap: Optional dictionary to receive the
        'node' : ['lv', ...] data.
    @rtype: dict or None
    @return: None if lvmap arg is given, otherwise, a dictionary of
        the form { 'node_uuid' : ['volume1', 'volume2', ...], ... };
        volumeN is of the form "vg_name/lv_name", compatible with
        GetVolumeList()

    """
    def _MapLVsByNode(lvmap, devices, node_uuid):
      """Recursive helper function."""
      if not node_uuid in lvmap:
        lvmap[node_uuid] = []

      for dev in devices:
        if dev.dev_type == constants.DT_PLAIN:
          lvmap[node_uuid].append(dev.logical_id[0] + "/" + dev.logical_id[1])

        elif dev.dev_type in constants.DTS_DRBD:
          if dev.children:
            _MapLVsByNode(lvmap, dev.children, dev.logical_id[0])
            _MapLVsByNode(lvmap, dev.children, dev.logical_id[1])

        elif dev.children:
          _MapLVsByNode(lvmap, dev.children, node_uuid)

    instance = self._UnlockedGetInstanceInfo(inst_uuid)
    if instance is None:
      raise errors.ConfigurationError("Unknown instance '%s'" % inst_uuid)

    if lvmap is None:
      lvmap = {}
      ret = lvmap
    else:
      ret = None

    _MapLVsByNode(lvmap,
                  self._UnlockedGetInstanceDisks(instance.uuid),
                  instance.primary_node)
    return ret

  @_ConfigSync(shared=1)
  def GetInstanceLVsByNode(self, inst_uuid, lvmap=None):
    """Provide a mapping of node to LVs a given instance owns.

    This is a simple wrapper over L{_UnlockedGetInstanceLVsByNode}

    """
    return self._UnlockedGetInstanceLVsByNode(inst_uuid, lvmap=lvmap)

  @_ConfigSync(shared=1)
  def GetGroupDiskParams(self, group):
    """Get the disk params populated with inherit chain.

    @type group: L{objects.NodeGroup}
    @param group: The group we want to know the params for
    @return: A dict with the filled in disk params

    """
    return self._UnlockedGetGroupDiskParams(group)

  def _UnlockedGetGroupDiskParams(self, group):
    """Get the disk params populated with inherit chain down to node-group.

    @type group: L{objects.NodeGroup}
    @param group: The group we want to know the params for
    @return: A dict with the filled in disk params

    """
    data = self._ConfigData().cluster.SimpleFillDP(group.diskparams)
    assert isinstance(data, dict), "Not a dictionary: " + str(data)
    return data

  @_ConfigSync(shared=1)
  def GetPotentialMasterCandidates(self):
    """Gets the list of node names of potential master candidates.

    @rtype: list of str
    @return: list of node names of potential master candidates

    """
    # FIXME: Note that currently potential master candidates are nodes
    # but this definition will be extended once RAPI-unmodifiable
    # parameters are introduced.
    nodes = self._UnlockedGetAllNodesInfo()
    return [node_info.name for node_info in nodes.values()]

  def GenerateMAC(self, net_uuid, _ec_id):
    """Generate a MAC for an instance.

    This should check the current instances for duplicates.

    """
    return self._wconfd.GenerateMAC(self._GetWConfdContext(), net_uuid)

  def ReserveMAC(self, mac, _ec_id):
    """Reserve a MAC for an instance.

    This only checks instances managed by this cluster, it does not
    check for potential collisions elsewhere.

    """
    self._wconfd.ReserveMAC(self._GetWConfdContext(), mac)

  def _UnlockedCommitTemporaryIps(self, _ec_id):
    """Commit all reserved IP address to their respective pools

    """
    if self._offline:
      raise errors.ProgrammerError("Can't call CommitTemporaryIps"
                                   " in offline mode")
    ips = self._wconfd.ListReservedIps(self._GetWConfdContext())
    for action, address, net_uuid in ips:
      self._UnlockedCommitIp(action, net_uuid, address)

  def _UnlockedCommitIp(self, action, net_uuid, address):
    """Commit a reserved IP address to an IP pool.

    The IP address is taken from the network's IP pool and marked as free.

    """
    nobj = self._UnlockedGetNetwork(net_uuid)
    if nobj is None:
      raise errors.ProgrammerError("Network '%s' not found" % (net_uuid, ))
    pool = network.AddressPool(nobj)
    if action == constants.RESERVE_ACTION:
      pool.Reserve(address)
    elif action == constants.RELEASE_ACTION:
      pool.Release(address)

  def ReleaseIp(self, net_uuid, address, _ec_id):
    """Give a specific IP address back to an IP pool.

    The IP address is returned to the IP pool and marked as reserved.

    """
    if net_uuid:
      if self._offline:
        raise errors.ProgrammerError("Can't call ReleaseIp in offline mode")
      self._wconfd.ReleaseIp(self._GetWConfdContext(), net_uuid, address)

  def GenerateIp(self, net_uuid, _ec_id):
    """Find a free IPv4 address for an instance.

    """
    if self._offline:
      raise errors.ProgrammerError("Can't call GenerateIp in offline mode")
    return self._wconfd.GenerateIp(self._GetWConfdContext(), net_uuid)

  def ReserveIp(self, net_uuid, address, _ec_id, check=True):
    """Reserve a given IPv4 address for use by an instance.

    """
    if self._offline:
      raise errors.ProgrammerError("Can't call ReserveIp in offline mode")
    return self._wconfd.ReserveIp(self._GetWConfdContext(), net_uuid, address,
                                  check)

  def ReserveLV(self, lv_name, _ec_id):
    """Reserve an VG/LV pair for an instance.

    @type lv_name: string
    @param lv_name: the logical volume name to reserve

    """
    return self._wconfd.ReserveLV(self._GetWConfdContext(), lv_name)

  def GenerateDRBDSecret(self, _ec_id):
    """Generate a DRBD secret.

    This checks the current disks for duplicates.

    """
    return self._wconfd.GenerateDRBDSecret(self._GetWConfdContext())

  # FIXME: After _AllIDs is removed, move it to config_mock.py
  def _AllLVs(self):
    """Compute the list of all LVs.

    """
    lvnames = set()
    for instance in self._ConfigData().instances.values():
      node_data = self._UnlockedGetInstanceLVsByNode(instance.uuid)
      for lv_list in node_data.values():
        lvnames.update(lv_list)
    return lvnames

  def _AllNICs(self):
    """Compute the list of all NICs.

    """
    nics = []
    for instance in self._ConfigData().instances.values():
      nics.extend(instance.nics)
    return nics

  def _AllIDs(self, include_temporary):
    """Compute the list of all UUIDs and names we have.

    @type include_temporary: boolean
    @param include_temporary: whether to include the _temporary_ids set
    @rtype: set
    @return: a set of IDs

    """
    existing = set()
    if include_temporary:
      existing.update(self._temporary_ids.GetReserved())
    existing.update(self._AllLVs())
    existing.update(self._ConfigData().instances.keys())
    existing.update(self._ConfigData().nodes.keys())
    existing.update([i.uuid for i in self._AllUUIDObjects() if i.uuid])
    return existing

  def _GenerateUniqueID(self, ec_id):
    """Generate an unique UUID.

    This checks the current node, instances and disk names for
    duplicates.

    @rtype: string
    @return: the unique id

    """
    existing = self._AllIDs(include_temporary=False)
    return self._temporary_ids.Generate(existing, utils.NewUUID, ec_id)

  @_ConfigSync(shared=1)
  def GenerateUniqueID(self, ec_id):
    """Generate an unique ID.

    This is just a wrapper over the unlocked version.

    @type ec_id: string
    @param ec_id: unique id for the job to reserve the id to

    """
    return self._GenerateUniqueID(ec_id)

  def _AllMACs(self):
    """Return all MACs present in the config.

    @rtype: list
    @return: the list of all MACs

    """
    result = []
    for instance in self._ConfigData().instances.values():
      for nic in instance.nics:
        result.append(nic.mac)

    return result

  def _AllDRBDSecrets(self):
    """Return all DRBD secrets present in the config.

    @rtype: list
    @return: the list of all DRBD secrets

    """
    def helper(disk, result):
      """Recursively gather secrets from this disk."""
      if disk.dev_type == constants.DT_DRBD8:
        result.append(disk.logical_id[5])
      if disk.children:
        for child in disk.children:
          helper(child, result)

    result = []
    for disk in self._ConfigData().disks.values():
      helper(disk, result)

    return result

  @staticmethod
  def _VerifyDisks(data, result):
    """Per-disk verification checks

    Extends L{result} with diagnostic information about the disks.

    @type data: see L{_ConfigData}
    @param data: configuration data

    @type result: list of strings
    @param result: list containing diagnostic messages

    """
    instance_disk_uuids = [d for insts in data.instances.values()
                           for d in insts.disks]
    for disk_uuid in data.disks:
      disk = data.disks[disk_uuid]
      result.extend(["disk %s error: %s" % (disk.uuid, msg)
                     for msg in disk.Verify()])
      if disk.uuid != disk_uuid:
        result.append("disk '%s' is indexed by wrong UUID '%s'" %
                      (disk.name, disk_uuid))
      if disk.uuid not in instance_disk_uuids:
        result.append("disk '%s' is not attached to any instance" %
                      disk.uuid)

  def _UnlockedVerifyConfig(self):
    """Verify function.

    @rtype: list
    @return: a list of error messages; a non-empty list signifies
        configuration errors

    """
    # pylint: disable=R0914
    result = []
    seen_macs = []
    ports = {}
    data = self._ConfigData()
    cluster = data.cluster

    # First call WConfd to perform its checks, if we're not offline
    if not self._offline:
      try:
        self._wconfd.VerifyConfig()
      except errors.ConfigVerifyError, err:
        try:
          for msg in err.args[1]:
            result.append(msg)
        except IndexError:
          pass

    def _helper(owner, attr, value, template):
      try:
        utils.ForceDictType(value, template)
      except errors.GenericError, err:
        result.append("%s has invalid %s: %s" % (owner, attr, err))

    def _helper_nic(owner, params):
      try:
        objects.NIC.CheckParameterSyntax(params)
      except errors.ConfigurationError, err:
        result.append("%s has invalid nicparams: %s" % (owner, err))

    def _helper_ipolicy(owner, ipolicy, iscluster):
      try:
        objects.InstancePolicy.CheckParameterSyntax(ipolicy, iscluster)
      except errors.ConfigurationError, err:
        result.append("%s has invalid instance policy: %s" % (owner, err))
      for key, value in ipolicy.items():
        if key == constants.ISPECS_MINMAX:
          for k in range(len(value)):
            _helper_ispecs(owner, "ipolicy/%s[%s]" % (key, k), value[k])
        elif key == constants.ISPECS_STD:
          _helper(owner, "ipolicy/" + key, value,
                  constants.ISPECS_PARAMETER_TYPES)
        else:
          # FIXME: assuming list type
          if key in constants.IPOLICY_PARAMETERS:
            exp_type = float
            # if the value is int, it can be converted into float
            convertible_types = [int]
          else:
            exp_type = list
            convertible_types = []
          # Try to convert from allowed types, if necessary.
          if any(isinstance(value, ct) for ct in convertible_types):
            try:
              value = exp_type(value)
              ipolicy[key] = value
            except ValueError:
              pass
          if not isinstance(value, exp_type):
            result.append("%s has invalid instance policy: for %s,"
                          " expecting %s, got %s" %
                          (owner, key, exp_type.__name__, type(value)))

    def _helper_ispecs(owner, parentkey, params):
      for (key, value) in params.items():
        fullkey = "/".join([parentkey, key])
        _helper(owner, fullkey, value, constants.ISPECS_PARAMETER_TYPES)

    # check cluster parameters
    _helper("cluster", "beparams", cluster.SimpleFillBE({}),
            constants.BES_PARAMETER_TYPES)
    _helper("cluster", "nicparams", cluster.SimpleFillNIC({}),
            constants.NICS_PARAMETER_TYPES)
    _helper_nic("cluster", cluster.SimpleFillNIC({}))
    _helper("cluster", "ndparams", cluster.SimpleFillND({}),
            constants.NDS_PARAMETER_TYPES)
    _helper_ipolicy("cluster", cluster.ipolicy, True)

    for disk_template in cluster.diskparams:
      if disk_template not in constants.DTS_HAVE_ACCESS:
        continue

      access = cluster.diskparams[disk_template].get(constants.LDP_ACCESS,
                                                     constants.DISK_KERNELSPACE)
      if access not in constants.DISK_VALID_ACCESS_MODES:
        result.append(
          "Invalid value of '%s:%s': '%s' (expected one of %s)" % (
            disk_template, constants.LDP_ACCESS, access,
            utils.CommaJoin(constants.DISK_VALID_ACCESS_MODES)
          )
        )

    self._VerifyDisks(data, result)

    # per-instance checks
    for instance_uuid in data.instances:
      instance = data.instances[instance_uuid]
      if instance.uuid != instance_uuid:
        result.append("instance '%s' is indexed by wrong UUID '%s'" %
                      (instance.name, instance_uuid))
      if instance.primary_node not in data.nodes:
        result.append("instance '%s' has invalid primary node '%s'" %
                      (instance.name, instance.primary_node))
      for snode in self._UnlockedGetInstanceSecondaryNodes(instance.uuid):
        if snode not in data.nodes:
          result.append("instance '%s' has invalid secondary node '%s'" %
                        (instance.name, snode))
      for idx, nic in enumerate(instance.nics):
        if nic.mac in seen_macs:
          result.append("instance '%s' has NIC %d mac %s duplicate" %
                        (instance.name, idx, nic.mac))
        else:
          seen_macs.append(nic.mac)
        if nic.nicparams:
          filled = cluster.SimpleFillNIC(nic.nicparams)
          owner = "instance %s nic %d" % (instance.name, idx)
          _helper(owner, "nicparams",
                  filled, constants.NICS_PARAMETER_TYPES)
          _helper_nic(owner, filled)

      # disk template checks
      if not instance.disk_template in data.cluster.enabled_disk_templates:
        result.append("instance '%s' uses the disabled disk template '%s'." %
                      (instance.name, instance.disk_template))

      # parameter checks
      if instance.beparams:
        _helper("instance %s" % instance.name, "beparams",
                cluster.FillBE(instance), constants.BES_PARAMETER_TYPES)

      # check that disks exists
      for disk_uuid in instance.disks:
        if disk_uuid not in data.disks:
          result.append("Instance '%s' has invalid disk '%s'" %
                        (instance.name, disk_uuid))

      instance_disks = self._UnlockedGetInstanceDisks(instance.uuid)
      # gather the drbd ports for duplicate checks
      for (idx, dsk) in enumerate(instance_disks):
        if dsk.dev_type in constants.DTS_DRBD:
          tcp_port = dsk.logical_id[2]
          if tcp_port not in ports:
            ports[tcp_port] = []
          ports[tcp_port].append((instance.name, "drbd disk %s" % idx))
      # gather network port reservation
      net_port = getattr(instance, "network_port", None)
      if net_port is not None:
        if net_port not in ports:
          ports[net_port] = []
        ports[net_port].append((instance.name, "network port"))

      wrong_names = _CheckInstanceDiskIvNames(instance_disks)
      if wrong_names:
        tmp = "; ".join(("name of disk %s should be '%s', but is '%s'" %
                         (idx, exp_name, actual_name))
                        for (idx, exp_name, actual_name) in wrong_names)

        result.append("Instance '%s' has wrongly named disks: %s" %
                      (instance.name, tmp))

    # cluster-wide pool of free ports
    for free_port in cluster.tcpudp_port_pool:
      if free_port not in ports:
        ports[free_port] = []
      ports[free_port].append(("cluster", "port marked as free"))

    # compute tcp/udp duplicate ports
    keys = ports.keys()
    keys.sort()
    for pnum in keys:
      pdata = ports[pnum]
      if len(pdata) > 1:
        txt = utils.CommaJoin(["%s/%s" % val for val in pdata])
        result.append("tcp/udp port %s has duplicates: %s" % (pnum, txt))

    # highest used tcp port check
    if keys:
      if keys[-1] > cluster.highest_used_port:
        result.append("Highest used port mismatch, saved %s, computed %s" %
                      (cluster.highest_used_port, keys[-1]))

    if not data.nodes[cluster.master_node].master_candidate:
      result.append("Master node is not a master candidate")

    # master candidate checks
    mc_now, mc_max, _ = self._UnlockedGetMasterCandidateStats()
    if mc_now < mc_max:
      result.append("Not enough master candidates: actual %d, target %d" %
                    (mc_now, mc_max))

    # node checks
    for node_uuid, node in data.nodes.items():
      if node.uuid != node_uuid:
        result.append("Node '%s' is indexed by wrong UUID '%s'" %
                      (node.name, node_uuid))
      if [node.master_candidate, node.drained, node.offline].count(True) > 1:
        result.append("Node %s state is invalid: master_candidate=%s,"
                      " drain=%s, offline=%s" %
                      (node.name, node.master_candidate, node.drained,
                       node.offline))
      if node.group not in data.nodegroups:
        result.append("Node '%s' has invalid group '%s'" %
                      (node.name, node.group))
      else:
        _helper("node %s" % node.name, "ndparams",
                cluster.FillND(node, data.nodegroups[node.group]),
                constants.NDS_PARAMETER_TYPES)
      used_globals = constants.NDC_GLOBALS.intersection(node.ndparams)
      if used_globals:
        result.append("Node '%s' has some global parameters set: %s" %
                      (node.name, utils.CommaJoin(used_globals)))

    # nodegroups checks
    nodegroups_names = set()
    for nodegroup_uuid in data.nodegroups:
      nodegroup = data.nodegroups[nodegroup_uuid]
      if nodegroup.uuid != nodegroup_uuid:
        result.append("node group '%s' (uuid: '%s') indexed by wrong uuid '%s'"
                      % (nodegroup.name, nodegroup.uuid, nodegroup_uuid))
      if utils.UUID_RE.match(nodegroup.name.lower()):
        result.append("node group '%s' (uuid: '%s') has uuid-like name" %
                      (nodegroup.name, nodegroup.uuid))
      if nodegroup.name in nodegroups_names:
        result.append("duplicate node group name '%s'" % nodegroup.name)
      else:
        nodegroups_names.add(nodegroup.name)
      group_name = "group %s" % nodegroup.name
      _helper_ipolicy(group_name, cluster.SimpleFillIPolicy(nodegroup.ipolicy),
                      False)
      if nodegroup.ndparams:
        _helper(group_name, "ndparams",
                cluster.SimpleFillND(nodegroup.ndparams),
                constants.NDS_PARAMETER_TYPES)

    # drbd minors check
    # FIXME: The check for DRBD map needs to be implemented in WConfd

    # IP checks
    default_nicparams = cluster.nicparams[constants.PP_DEFAULT]
    ips = {}

    def _AddIpAddress(ip, name):
      ips.setdefault(ip, []).append(name)

    _AddIpAddress(cluster.master_ip, "cluster_ip")

    for node in data.nodes.values():
      _AddIpAddress(node.primary_ip, "node:%s/primary" % node.name)
      if node.secondary_ip != node.primary_ip:
        _AddIpAddress(node.secondary_ip, "node:%s/secondary" % node.name)

    for instance in data.instances.values():
      for idx, nic in enumerate(instance.nics):
        if nic.ip is None:
          continue

        nicparams = objects.FillDict(default_nicparams, nic.nicparams)
        nic_mode = nicparams[constants.NIC_MODE]
        nic_link = nicparams[constants.NIC_LINK]

        if nic_mode == constants.NIC_MODE_BRIDGED:
          link = "bridge:%s" % nic_link
        elif nic_mode == constants.NIC_MODE_ROUTED:
          link = "route:%s" % nic_link
        elif nic_mode == constants.NIC_MODE_OVS:
          link = "ovs:%s" % nic_link
        else:
          raise errors.ProgrammerError("NIC mode '%s' not handled" % nic_mode)

        _AddIpAddress("%s/%s/%s" % (link, nic.ip, nic.network),
                      "instance:%s/nic:%d" % (instance.name, idx))

    for ip, owners in ips.items():
      if len(owners) > 1:
        result.append("IP address %s is used by multiple owners: %s" %
                      (ip, utils.CommaJoin(owners)))

    return result

  def _UnlockedVerifyConfigAndLog(self, feedback_fn=None):
    """Verify the configuration and log any errors.

    The errors get logged as critical errors and also to the feedback function,
    if given.

    @param feedback_fn: Callable feedback function
    @rtype: list
    @return: a list of error messages; a non-empty list signifies
        configuration errors

    """
    assert feedback_fn is None or callable(feedback_fn)

    # Warn on config errors, but don't abort the save - the
    # configuration has already been modified, and we can't revert;
    # the best we can do is to warn the user and save as is, leaving
    # recovery to the user
    config_errors = self._UnlockedVerifyConfig()
    if config_errors:
      errmsg = ("Configuration data is not consistent: %s" %
                (utils.CommaJoin(config_errors)))
      logging.critical(errmsg)
      if feedback_fn:
        feedback_fn(errmsg)
    return config_errors

  @_ConfigSync(shared=1)
  def VerifyConfig(self):
    """Verify function.

    This is just a wrapper over L{_UnlockedVerifyConfig}.

    @rtype: list
    @return: a list of error messages; a non-empty list signifies
        configuration errors

    """
    return self._UnlockedVerifyConfig()

  @_ConfigSync()
  def AddTcpUdpPort(self, port):
    """Adds a new port to the available port pool.

    @warning: this method does not "flush" the configuration (via
        L{_WriteConfig}); callers should do that themselves once the
        configuration is stable

    """
    if not isinstance(port, int):
      raise errors.ProgrammerError("Invalid type passed for port")

    self._ConfigData().cluster.tcpudp_port_pool.add(port)

  @_ConfigSync(shared=1)
  def GetPortList(self):
    """Returns a copy of the current port list.

    """
    return self._ConfigData().cluster.tcpudp_port_pool.copy()

  @_ConfigSync()
  def AllocatePort(self):
    """Allocate a port.

    The port will be taken from the available port pool or from the
    default port range (and in this case we increase
    highest_used_port).

    """
    # If there are TCP/IP ports configured, we use them first.
    if self._ConfigData().cluster.tcpudp_port_pool:
      port = self._ConfigData().cluster.tcpudp_port_pool.pop()
    else:
      port = self._ConfigData().cluster.highest_used_port + 1
      if port >= constants.LAST_DRBD_PORT:
        raise errors.ConfigurationError("The highest used port is greater"
                                        " than %s. Aborting." %
                                        constants.LAST_DRBD_PORT)
      self._ConfigData().cluster.highest_used_port = port
    return port

  @_ConfigSync()
  def ComputeDRBDMap(self):
    """Compute the used DRBD minor/nodes.

    This is just a wrapper over a call to WConfd.

    @return: dictionary of node_uuid: dict of minor: instance_uuid;
        the returned dict will have all the nodes in it (even if with
        an empty list).

    """
    if self._offline:
      raise errors.ProgrammerError("Can't call ComputeDRBDMap in offline mode")
    else:
      return dict(map(lambda (k, v): (k, dict(v)),
                      self._wconfd.ComputeDRBDMap()))

  def AllocateDRBDMinor(self, node_uuids, inst_uuid):
    """Allocate a drbd minor.

    This is just a wrapper over a call to WConfd.

    The free minor will be automatically computed from the existing
    devices. A node can be given multiple times in order to allocate
    multiple minors. The result is the list of minors, in the same
    order as the passed nodes.

    @type inst_uuid: string
    @param inst_uuid: the instance for which we allocate minors

    """
    assert isinstance(inst_uuid, basestring), \
           "Invalid argument '%s' passed to AllocateDRBDMinor" % inst_uuid

    if self._offline:
      raise errors.ProgrammerError("Can't call AllocateDRBDMinor"
                                   " in offline mode")

    result = self._wconfd.AllocateDRBDMinor(inst_uuid, node_uuids)
    logging.debug("Request to allocate drbd minors, input: %s, returning %s",
                  node_uuids, result)
    return result

  def _UnlockedReleaseDRBDMinors(self, inst_uuid):
    """Release temporary drbd minors allocated for a given instance.

    This is just a wrapper over a call to WConfd.

    @type inst_uuid: string
    @param inst_uuid: the instance for which temporary minors should be
                      released

    """
    assert isinstance(inst_uuid, basestring), \
           "Invalid argument passed to ReleaseDRBDMinors"
    # in offline mode we allow the calls to release DRBD minors,
    # because then nothing can be allocated anyway;
    # this is useful for testing
    if not self._offline:
      self._wconfd.ReleaseDRBDMinors(inst_uuid)

  @_ConfigSync()
  def ReleaseDRBDMinors(self, inst_uuid):
    """Release temporary drbd minors allocated for a given instance.

    This should be called on the error paths, on the success paths
    it's automatically called by the ConfigWriter add and update
    functions.

    This function is just a wrapper over L{_UnlockedReleaseDRBDMinors}.

    @type inst_uuid: string
    @param inst_uuid: the instance for which temporary minors should be
                      released

    """
    self._UnlockedReleaseDRBDMinors(inst_uuid)

  @_ConfigSync(shared=1)
  def GetConfigVersion(self):
    """Get the configuration version.

    @return: Config version

    """
    return self._ConfigData().version

  @_ConfigSync(shared=1)
  def GetClusterName(self):
    """Get cluster name.

    @return: Cluster name

    """
    return self._ConfigData().cluster.cluster_name

  @_ConfigSync(shared=1)
  def GetMasterNode(self):
    """Get the UUID of the master node for this cluster.

    @return: Master node UUID

    """
    return self._ConfigData().cluster.master_node

  @_ConfigSync(shared=1)
  def GetMasterNodeName(self):
    """Get the hostname of the master node for this cluster.

    @return: Master node hostname

    """
    return self._UnlockedGetNodeName(self._ConfigData().cluster.master_node)

  @_ConfigSync(shared=1)
  def GetMasterNodeInfo(self):
    """Get the master node information for this cluster.

    @rtype: objects.Node
    @return: Master node L{objects.Node} object

    """
    return self._UnlockedGetNodeInfo(self._ConfigData().cluster.master_node)

  @_ConfigSync(shared=1)
  def GetMasterIP(self):
    """Get the IP of the master node for this cluster.

    @return: Master IP

    """
    return self._ConfigData().cluster.master_ip

  @_ConfigSync(shared=1)
  def GetMasterNetdev(self):
    """Get the master network device for this cluster.

    """
    return self._ConfigData().cluster.master_netdev

  @_ConfigSync(shared=1)
  def GetMasterNetmask(self):
    """Get the netmask of the master node for this cluster.

    """
    return self._ConfigData().cluster.master_netmask

  @_ConfigSync(shared=1)
  def GetUseExternalMipScript(self):
    """Get flag representing whether to use the external master IP setup script.

    """
    return self._ConfigData().cluster.use_external_mip_script

  @_ConfigSync(shared=1)
  def GetFileStorageDir(self):
    """Get the file storage dir for this cluster.

    """
    return self._ConfigData().cluster.file_storage_dir

  @_ConfigSync(shared=1)
  def GetSharedFileStorageDir(self):
    """Get the shared file storage dir for this cluster.

    """
    return self._ConfigData().cluster.shared_file_storage_dir

  @_ConfigSync(shared=1)
  def GetGlusterStorageDir(self):
    """Get the Gluster storage dir for this cluster.

    """
    return self._ConfigData().cluster.gluster_storage_dir

  @_ConfigSync(shared=1)
  def GetHypervisorType(self):
    """Get the hypervisor type for this cluster.

    """
    return self._ConfigData().cluster.enabled_hypervisors[0]

  @_ConfigSync(shared=1)
  def GetRsaHostKey(self):
    """Return the rsa hostkey from the config.

    @rtype: string
    @return: the rsa hostkey

    """
    return self._ConfigData().cluster.rsahostkeypub

  @_ConfigSync(shared=1)
  def GetDsaHostKey(self):
    """Return the dsa hostkey from the config.

    @rtype: string
    @return: the dsa hostkey

    """
    return self._ConfigData().cluster.dsahostkeypub

  @_ConfigSync(shared=1)
  def GetDefaultIAllocator(self):
    """Get the default instance allocator for this cluster.

    """
    return self._ConfigData().cluster.default_iallocator

  @_ConfigSync(shared=1)
  def GetDefaultIAllocatorParameters(self):
    """Get the default instance allocator parameters for this cluster.

    @rtype: dict
    @return: dict of iallocator parameters

    """
    return self._ConfigData().cluster.default_iallocator_params

  @_ConfigSync(shared=1)
  def GetPrimaryIPFamily(self):
    """Get cluster primary ip family.

    @return: primary ip family

    """
    return self._ConfigData().cluster.primary_ip_family

  @_ConfigSync(shared=1)
  def GetMasterNetworkParameters(self):
    """Get network parameters of the master node.

    @rtype: L{object.MasterNetworkParameters}
    @return: network parameters of the master node

    """
    cluster = self._ConfigData().cluster
    result = objects.MasterNetworkParameters(
      uuid=cluster.master_node, ip=cluster.master_ip,
      netmask=cluster.master_netmask, netdev=cluster.master_netdev,
      ip_family=cluster.primary_ip_family)

    return result

  @_ConfigSync(shared=1)
  def GetInstallImage(self):
    """Get the install image location

    @rtype: string
    @return: location of the install image

    """
    return self._ConfigData().cluster.install_image

  @_ConfigSync()
  def SetInstallImage(self, install_image):
    """Set the install image location

    @type install_image: string
    @param install_image: location of the install image

    """
    self._ConfigData().cluster.install_image = install_image

  @_ConfigSync(shared=1)
  def GetInstanceCommunicationNetwork(self):
    """Get cluster instance communication network

    @rtype: string
    @return: instance communication network, which is the name of the
             network used for instance communication

    """
    return self._ConfigData().cluster.instance_communication_network

  @_ConfigSync()
  def SetInstanceCommunicationNetwork(self, network_name):
    """Set cluster instance communication network

    @type network_name: string
    @param network_name: instance communication network, which is the name of
                         the network used for instance communication

    """
    self._ConfigData().cluster.instance_communication_network = network_name

  @_ConfigSync(shared=1)
  def GetZeroingImage(self):
    """Get the zeroing image location

    @rtype: string
    @return: the location of the zeroing image

    """
    return self._config_data.cluster.zeroing_image

  @_ConfigSync(shared=1)
  def GetCompressionTools(self):
    """Get cluster compression tools

    @rtype: list of string
    @return: a list of tools that are cleared for use in this cluster for the
             purpose of compressing data

    """
    return self._ConfigData().cluster.compression_tools

  @_ConfigSync()
  def SetCompressionTools(self, tools):
    """Set cluster compression tools

    @type tools: list of string
    @param tools: a list of tools that are cleared for use in this cluster for
                  the purpose of compressing data

    """
    self._ConfigData().cluster.compression_tools = tools

  @_ConfigSync()
  def AddNodeGroup(self, group, ec_id, check_uuid=True):
    """Add a node group to the configuration.

    This method calls group.UpgradeConfig() to fill any missing attributes
    according to their default values.

    @type group: L{objects.NodeGroup}
    @param group: the NodeGroup object to add
    @type ec_id: string
    @param ec_id: unique id for the job to use when creating a missing UUID
    @type check_uuid: bool
    @param check_uuid: add an UUID to the group if it doesn't have one or, if
                       it does, ensure that it does not exist in the
                       configuration already

    """
    self._UnlockedAddNodeGroup(group, ec_id, check_uuid)

  def _UnlockedAddNodeGroup(self, group, ec_id, check_uuid):
    """Add a node group to the configuration.

    """
    logging.info("Adding node group %s to configuration", group.name)

    # Some code might need to add a node group with a pre-populated UUID
    # generated with ConfigWriter.GenerateUniqueID(). We allow them to bypass
    # the "does this UUID" exist already check.
    if check_uuid:
      self._EnsureUUID(group, ec_id)

    try:
      existing_uuid = self._UnlockedLookupNodeGroup(group.name)
    except errors.OpPrereqError:
      pass
    else:
      raise errors.OpPrereqError("Desired group name '%s' already exists as a"
                                 " node group (UUID: %s)" %
                                 (group.name, existing_uuid),
                                 errors.ECODE_EXISTS)

    group.serial_no = 1
    group.ctime = group.mtime = time.time()
    group.UpgradeConfig()

    self._ConfigData().nodegroups[group.uuid] = group
    self._ConfigData().cluster.serial_no += 1

  @_ConfigSync()
  def RemoveNodeGroup(self, group_uuid):
    """Remove a node group from the configuration.

    @type group_uuid: string
    @param group_uuid: the UUID of the node group to remove

    """
    logging.info("Removing node group %s from configuration", group_uuid)

    if group_uuid not in self._ConfigData().nodegroups:
      raise errors.ConfigurationError("Unknown node group '%s'" % group_uuid)

    assert len(self._ConfigData().nodegroups) != 1, \
            "Group '%s' is the only group, cannot be removed" % group_uuid

    del self._ConfigData().nodegroups[group_uuid]
    self._ConfigData().cluster.serial_no += 1

  def _UnlockedLookupNodeGroup(self, target):
    """Lookup a node group's UUID.

    @type target: string or None
    @param target: group name or UUID or None to look for the default
    @rtype: string
    @return: nodegroup UUID
    @raises errors.OpPrereqError: when the target group cannot be found

    """
    if target is None:
      if len(self._ConfigData().nodegroups) != 1:
        raise errors.OpPrereqError("More than one node group exists. Target"
                                   " group must be specified explicitly.")
      else:
        return self._ConfigData().nodegroups.keys()[0]
    if target in self._ConfigData().nodegroups:
      return target
    for nodegroup in self._ConfigData().nodegroups.values():
      if nodegroup.name == target:
        return nodegroup.uuid
    raise errors.OpPrereqError("Node group '%s' not found" % target,
                               errors.ECODE_NOENT)

  @_ConfigSync(shared=1)
  def LookupNodeGroup(self, target):
    """Lookup a node group's UUID.

    This function is just a wrapper over L{_UnlockedLookupNodeGroup}.

    @type target: string or None
    @param target: group name or UUID or None to look for the default
    @rtype: string
    @return: nodegroup UUID

    """
    return self._UnlockedLookupNodeGroup(target)

  def _UnlockedGetNodeGroup(self, uuid):
    """Lookup a node group.

    @type uuid: string
    @param uuid: group UUID
    @rtype: L{objects.NodeGroup} or None
    @return: nodegroup object, or None if not found

    """
    if uuid not in self._ConfigData().nodegroups:
      return None

    return self._ConfigData().nodegroups[uuid]

  @_ConfigSync(shared=1)
  def GetNodeGroup(self, uuid):
    """Lookup a node group.

    @type uuid: string
    @param uuid: group UUID
    @rtype: L{objects.NodeGroup} or None
    @return: nodegroup object, or None if not found

    """
    return self._UnlockedGetNodeGroup(uuid)

  def _UnlockedGetAllNodeGroupsInfo(self):
    """Get the configuration of all node groups.

    """
    return dict(self._ConfigData().nodegroups)

  @_ConfigSync(shared=1)
  def GetAllNodeGroupsInfo(self):
    """Get the configuration of all node groups.

    """
    return self._UnlockedGetAllNodeGroupsInfo()

  @_ConfigSync(shared=1)
  def GetAllNodeGroupsInfoDict(self):
    """Get the configuration of all node groups expressed as a dictionary of
    dictionaries.

    """
    return dict(map(lambda (uuid, ng): (uuid, ng.ToDict()),
                    self._UnlockedGetAllNodeGroupsInfo().items()))

  @_ConfigSync(shared=1)
  def GetNodeGroupList(self):
    """Get a list of node groups.

    """
    return self._ConfigData().nodegroups.keys()

  @_ConfigSync(shared=1)
  def GetNodeGroupMembersByNodes(self, nodes):
    """Get nodes which are member in the same nodegroups as the given nodes.

    """
    ngfn = lambda node_uuid: self._UnlockedGetNodeInfo(node_uuid).group
    return frozenset(member_uuid
                     for node_uuid in nodes
                     for member_uuid in
                       self._UnlockedGetNodeGroup(ngfn(node_uuid)).members)

  @_ConfigSync(shared=1)
  def GetMultiNodeGroupInfo(self, group_uuids):
    """Get the configuration of multiple node groups.

    @param group_uuids: List of node group UUIDs
    @rtype: list
    @return: List of tuples of (group_uuid, group_info)

    """
    return [(uuid, self._UnlockedGetNodeGroup(uuid)) for uuid in group_uuids]

  def AddInstance(self, instance, _ec_id):
    """Add an instance to the config.

    This should be used after creating a new instance.

    @type instance: L{objects.Instance}
    @param instance: the instance object

    """
    if not isinstance(instance, objects.Instance):
      raise errors.ProgrammerError("Invalid type passed to AddInstance")

    all_macs = self._AllMACs()
    for nic in instance.nics:
      if nic.mac in all_macs:
        raise errors.ConfigurationError("Cannot add instance %s:"
                                        " MAC address '%s' already in use." %
                                        (instance.name, nic.mac))

    self._CheckUniqueUUID(instance, include_temporary=False)

    instance.serial_no = 1
    instance.ctime = instance.mtime = time.time()

    utils.SimpleRetry(True, self._wconfd.AddInstance, 0.1, 30,
                      args=[instance.ToDict(), self._GetWConfdContext()])
    self.OutDate()

  def _EnsureUUID(self, item, ec_id):
    """Ensures a given object has a valid UUID.

    @param item: the instance or node to be checked
    @param ec_id: the execution context id for the uuid reservation

    """
    if not item.uuid:
      item.uuid = self._GenerateUniqueID(ec_id)
    else:
      self._CheckUniqueUUID(item, include_temporary=True)

  def _CheckUniqueUUID(self, item, include_temporary):
    """Checks that the UUID of the given object is unique.

    @param item: the instance or node to be checked
    @param include_temporary: whether temporarily generated UUID's should be
              included in the check. If the UUID of the item to be checked is
              a temporarily generated one, this has to be C{False}.

    """
    if not item.uuid:
      raise errors.ConfigurationError("'%s' must have an UUID" % (item.name,))
    if item.uuid in self._AllIDs(include_temporary=include_temporary):
      raise errors.ConfigurationError("Cannot add '%s': UUID %s already"
                                      " in use" % (item.name, item.uuid))

  def _SetInstanceStatus(self, inst_uuid, status, disks_active,
                         admin_state_source):
    """Set the instance's status to a given value.

    @rtype: L{objects.Instance}
    @return: the updated instance object

    """
    if inst_uuid not in self._ConfigData().instances:
      raise errors.ConfigurationError("Unknown instance '%s'" %
                                      inst_uuid)
    instance = self._ConfigData().instances[inst_uuid]

    if status is None:
      status = instance.admin_state
    if disks_active is None:
      disks_active = instance.disks_active
    if admin_state_source is None:
      admin_state_source = instance.admin_state_source

    assert status in constants.ADMINST_ALL, \
           "Invalid status '%s' passed to SetInstanceStatus" % (status,)

    if instance.admin_state != status or \
       instance.disks_active != disks_active or \
       instance.admin_state_source != admin_state_source:
      instance.admin_state = status
      instance.disks_active = disks_active
      instance.admin_state_source = admin_state_source
      instance.serial_no += 1
      instance.mtime = time.time()
    return instance

  @_ConfigSync()
  def MarkInstanceUp(self, inst_uuid):
    """Mark the instance status to up in the config.

    This also sets the instance disks active flag.

    @rtype: L{objects.Instance}
    @return: the updated instance object

    """
    return self._SetInstanceStatus(inst_uuid, constants.ADMINST_UP, True,
                                   constants.ADMIN_SOURCE)

  @_ConfigSync()
  def MarkInstanceOffline(self, inst_uuid):
    """Mark the instance status to down in the config.

    This also clears the instance disks active flag.

    @rtype: L{objects.Instance}
    @return: the updated instance object

    """
    return self._SetInstanceStatus(inst_uuid, constants.ADMINST_OFFLINE, False,
                                   constants.ADMIN_SOURCE)

  @_ConfigSync()
  def RemoveInstance(self, inst_uuid):
    """Remove the instance from the configuration.

    """
    if inst_uuid not in self._ConfigData().instances:
      raise errors.ConfigurationError("Unknown instance '%s'" % inst_uuid)

    # If a network port has been allocated to the instance,
    # return it to the pool of free ports.
    inst = self._ConfigData().instances[inst_uuid]
    network_port = getattr(inst, "network_port", None)
    if network_port is not None:
      self._ConfigData().cluster.tcpudp_port_pool.add(network_port)

    instance = self._UnlockedGetInstanceInfo(inst_uuid)

    # FIXME: After RemoveInstance is moved to WConfd, use its internal
    # function from TempRes module.
    for nic in instance.nics:
      if nic.network and nic.ip:
        # Return all IP addresses to the respective address pools
        self._UnlockedCommitIp(constants.RELEASE_ACTION, nic.network, nic.ip)

    del self._ConfigData().instances[inst_uuid]
    self._ConfigData().cluster.serial_no += 1

  @_ConfigSync()
  def RenameInstance(self, inst_uuid, new_name):
    """Rename an instance.

    This needs to be done in ConfigWriter and not by RemoveInstance
    combined with AddInstance as only we can guarantee an atomic
    rename.

    """
    if inst_uuid not in self._ConfigData().instances:
      raise errors.ConfigurationError("Unknown instance '%s'" % inst_uuid)

    inst = self._ConfigData().instances[inst_uuid]
    inst.name = new_name

    instance_disks = self._UnlockedGetInstanceDisks(inst_uuid)
    for (_, disk) in enumerate(instance_disks):
      if disk.dev_type in [constants.DT_FILE, constants.DT_SHARED_FILE]:
        # rename the file paths in logical and physical id
        file_storage_dir = os.path.dirname(os.path.dirname(disk.logical_id[1]))
        disk.logical_id = (disk.logical_id[0],
                           utils.PathJoin(file_storage_dir, inst.name,
                                          os.path.basename(disk.logical_id[1])))

    # Force update of ssconf files
    self._ConfigData().cluster.serial_no += 1

  @_ConfigSync()
  def MarkInstanceDown(self, inst_uuid):
    """Mark the status of an instance to down in the configuration.

    This does not touch the instance disks active flag, as shut down instances
    can still have active disks.

    @rtype: L{objects.Instance}
    @return: the updated instance object

    """
    return self._SetInstanceStatus(inst_uuid, constants.ADMINST_DOWN, None,
                                   constants.ADMIN_SOURCE)

  @_ConfigSync()
  def MarkInstanceUserDown(self, inst_uuid):
    """Mark the status of an instance to user down in the configuration.

    This does not touch the instance disks active flag, as user shut
    down instances can still have active disks.

    """

    self._SetInstanceStatus(inst_uuid, constants.ADMINST_DOWN, None,
                            constants.USER_SOURCE)

  @_ConfigSync()
  def MarkInstanceDisksActive(self, inst_uuid):
    """Mark the status of instance disks active.

    @rtype: L{objects.Instance}
    @return: the updated instance object

    """
    return self._SetInstanceStatus(inst_uuid, None, True, None)

  @_ConfigSync()
  def MarkInstanceDisksInactive(self, inst_uuid):
    """Mark the status of instance disks inactive.

    @rtype: L{objects.Instance}
    @return: the updated instance object

    """
    return self._SetInstanceStatus(inst_uuid, None, False, None)

  def _UnlockedGetInstanceList(self):
    """Get the list of instances.

    This function is for internal use, when the config lock is already held.

    """
    return self._ConfigData().instances.keys()

  @_ConfigSync(shared=1)
  def GetInstanceList(self):
    """Get the list of instances.

    @return: array of instances, ex. ['instance2-uuid', 'instance1-uuid']

    """
    return self._UnlockedGetInstanceList()

  def ExpandInstanceName(self, short_name):
    """Attempt to expand an incomplete instance name.

    """
    # Locking is done in L{ConfigWriter.GetAllInstancesInfo}
    all_insts = self.GetAllInstancesInfo().values()
    expanded_name = _MatchNameComponentIgnoreCase(
                      short_name, [inst.name for inst in all_insts])

    if expanded_name is not None:
      # there has to be exactly one instance with that name
      inst = (filter(lambda n: n.name == expanded_name, all_insts)[0])
      return (inst.uuid, inst.name)
    else:
      return (None, None)

  def _UnlockedGetInstanceInfo(self, inst_uuid):
    """Returns information about an instance.

    This function is for internal use, when the config lock is already held.

    """
    if inst_uuid not in self._ConfigData().instances:
      return None

    return self._ConfigData().instances[inst_uuid]

  @_ConfigSync(shared=1)
  def GetInstanceInfo(self, inst_uuid):
    """Returns information about an instance.

    It takes the information from the configuration file. Other information of
    an instance are taken from the live systems.

    @param inst_uuid: UUID of the instance

    @rtype: L{objects.Instance}
    @return: the instance object

    """
    return self._UnlockedGetInstanceInfo(inst_uuid)

  @_ConfigSync(shared=1)
  def GetInstanceNodeGroups(self, inst_uuid, primary_only=False):
    """Returns set of node group UUIDs for instance's nodes.

    @rtype: frozenset

    """
    instance = self._UnlockedGetInstanceInfo(inst_uuid)
    if not instance:
      raise errors.ConfigurationError("Unknown instance '%s'" % inst_uuid)

    if primary_only:
      nodes = [instance.primary_node]
    else:
      nodes = self._UnlockedGetInstanceNodes(instance.uuid)

    return frozenset(self._UnlockedGetNodeInfo(node_uuid).group
                     for node_uuid in nodes)

  @_ConfigSync(shared=1)
  def GetInstanceNetworks(self, inst_uuid):
    """Returns set of network UUIDs for instance's nics.

    @rtype: frozenset

    """
    instance = self._UnlockedGetInstanceInfo(inst_uuid)
    if not instance:
      raise errors.ConfigurationError("Unknown instance '%s'" % inst_uuid)

    networks = set()
    for nic in instance.nics:
      if nic.network:
        networks.add(nic.network)

    return frozenset(networks)

  @_ConfigSync(shared=1)
  def GetMultiInstanceInfo(self, inst_uuids):
    """Get the configuration of multiple instances.

    @param inst_uuids: list of instance UUIDs
    @rtype: list
    @return: list of tuples (instance UUID, instance_info), where
        instance_info is what would GetInstanceInfo return for the
        node, while keeping the original order

    """
    return [(uuid, self._UnlockedGetInstanceInfo(uuid)) for uuid in inst_uuids]

  @_ConfigSync(shared=1)
  def GetMultiInstanceInfoByName(self, inst_names):
    """Get the configuration of multiple instances.

    @param inst_names: list of instance names
    @rtype: list
    @return: list of tuples (instance, instance_info), where
        instance_info is what would GetInstanceInfo return for the
        node, while keeping the original order

    """
    result = []
    for name in inst_names:
      instance = self._UnlockedGetInstanceInfoByName(name)
      result.append((instance.uuid, instance))
    return result

  @_ConfigSync(shared=1)
  def GetAllInstancesInfo(self):
    """Get the configuration of all instances.

    @rtype: dict
    @return: dict of (instance, instance_info), where instance_info is what
              would GetInstanceInfo return for the node

    """
    return self._UnlockedGetAllInstancesInfo()

  def _UnlockedGetAllInstancesInfo(self):
    my_dict = dict([(inst_uuid, self._UnlockedGetInstanceInfo(inst_uuid))
                    for inst_uuid in self._UnlockedGetInstanceList()])
    return my_dict

  @_ConfigSync(shared=1)
  def GetInstancesInfoByFilter(self, filter_fn):
    """Get instance configuration with a filter.

    @type filter_fn: callable
    @param filter_fn: Filter function receiving instance object as parameter,
      returning boolean. Important: this function is called while the
      configuration locks is held. It must not do any complex work or call
      functions potentially leading to a deadlock. Ideally it doesn't call any
      other functions and just compares instance attributes.

    """
    return dict((uuid, inst)
                for (uuid, inst) in self._ConfigData().instances.items()
                if filter_fn(inst))

  @_ConfigSync(shared=1)
  def GetInstanceInfoByName(self, inst_name):
    """Get the L{objects.Instance} object for a named instance.

    @param inst_name: name of the instance to get information for
    @type inst_name: string
    @return: the corresponding L{objects.Instance} instance or None if no
          information is available

    """
    return self._UnlockedGetInstanceInfoByName(inst_name)

  def _UnlockedGetInstanceInfoByName(self, inst_name):
    for inst in self._UnlockedGetAllInstancesInfo().values():
      if inst.name == inst_name:
        return inst
    return None

  def _UnlockedGetInstanceName(self, inst_uuid):
    inst_info = self._UnlockedGetInstanceInfo(inst_uuid)
    if inst_info is None:
      raise errors.OpExecError("Unknown instance: %s" % inst_uuid)
    return inst_info.name

  @_ConfigSync(shared=1)
  def GetInstanceName(self, inst_uuid):
    """Gets the instance name for the passed instance.

    @param inst_uuid: instance UUID to get name for
    @type inst_uuid: string
    @rtype: string
    @return: instance name

    """
    return self._UnlockedGetInstanceName(inst_uuid)

  @_ConfigSync(shared=1)
  def GetInstanceNames(self, inst_uuids):
    """Gets the instance names for the passed list of nodes.

    @param inst_uuids: list of instance UUIDs to get names for
    @type inst_uuids: list of strings
    @rtype: list of strings
    @return: list of instance names

    """
    return self._UnlockedGetInstanceNames(inst_uuids)

  @_ConfigSync()
  def SetInstancePrimaryNode(self, inst_uuid, target_node_uuid):
    """Sets the primary node of an existing instance

    @param inst_uuid: instance UUID
    @type inst_uuid: string
    @param target_node_uuid: the new primary node UUID
    @type target_node_uuid: string

    """
    self._UnlockedGetInstanceInfo(inst_uuid).primary_node = target_node_uuid

  @_ConfigSync()
  def SetDiskNodes(self, disk_uuid, nodes):
    """Sets the nodes of an existing disk

    @param disk_uuid: disk UUID
    @type disk_uuid: string
    @param nodes: the new nodes for the disk
    @type nodes: list of node uuids

    """
    self._UnlockedGetDiskInfo(disk_uuid).nodes = nodes

  def _UnlockedGetInstanceNames(self, inst_uuids):
    return [self._UnlockedGetInstanceName(uuid) for uuid in inst_uuids]

  def _UnlockedAddNode(self, node, ec_id):
    """Add a node to the configuration.

    @type node: L{objects.Node}
    @param node: a Node instance

    """
    logging.info("Adding node %s to configuration", node.name)

    self._EnsureUUID(node, ec_id)

    node.serial_no = 1
    node.ctime = node.mtime = time.time()
    self._UnlockedAddNodeToGroup(node.uuid, node.group)
    assert node.uuid in self._ConfigData().nodegroups[node.group].members
    self._ConfigData().nodes[node.uuid] = node
    self._ConfigData().cluster.serial_no += 1

  @_ConfigSync()
  def AddNode(self, node, ec_id):
    """Add a node to the configuration.

    @type node: L{objects.Node}
    @param node: a Node instance

    """
    self._UnlockedAddNode(node, ec_id)

  @_ConfigSync()
  def RemoveNode(self, node_uuid):
    """Remove a node from the configuration.

    """
    logging.info("Removing node %s from configuration", node_uuid)

    if node_uuid not in self._ConfigData().nodes:
      raise errors.ConfigurationError("Unknown node '%s'" % node_uuid)

    self._UnlockedRemoveNodeFromGroup(self._ConfigData().nodes[node_uuid])
    del self._ConfigData().nodes[node_uuid]
    self._ConfigData().cluster.serial_no += 1

  def ExpandNodeName(self, short_name):
    """Attempt to expand an incomplete node name into a node UUID.

    """
    # Locking is done in L{ConfigWriter.GetAllNodesInfo}
    all_nodes = self.GetAllNodesInfo().values()
    expanded_name = _MatchNameComponentIgnoreCase(
                      short_name, [node.name for node in all_nodes])

    if expanded_name is not None:
      # there has to be exactly one node with that name
      node = (filter(lambda n: n.name == expanded_name, all_nodes)[0])
      return (node.uuid, node.name)
    else:
      return (None, None)

  def _UnlockedGetNodeInfo(self, node_uuid):
    """Get the configuration of a node, as stored in the config.

    This function is for internal use, when the config lock is already
    held.

    @param node_uuid: the node UUID

    @rtype: L{objects.Node}
    @return: the node object

    """
    if node_uuid not in self._ConfigData().nodes:
      return None

    return self._ConfigData().nodes[node_uuid]

  @_ConfigSync(shared=1)
  def GetNodeInfo(self, node_uuid):
    """Get the configuration of a node, as stored in the config.

    This is just a locked wrapper over L{_UnlockedGetNodeInfo}.

    @param node_uuid: the node UUID

    @rtype: L{objects.Node}
    @return: the node object

    """
    return self._UnlockedGetNodeInfo(node_uuid)

  @_ConfigSync(shared=1)
  def GetNodeInstances(self, node_uuid):
    """Get the instances of a node, as stored in the config.

    @param node_uuid: the node UUID

    @rtype: (list, list)
    @return: a tuple with two lists: the primary and the secondary instances

    """
    pri = []
    sec = []
    for inst in self._ConfigData().instances.values():
      if inst.primary_node == node_uuid:
        pri.append(inst.uuid)
      if node_uuid in self._UnlockedGetInstanceSecondaryNodes(inst.uuid):
        sec.append(inst.uuid)
    return (pri, sec)

  @_ConfigSync(shared=1)
  def GetNodeGroupInstances(self, uuid, primary_only=False):
    """Get the instances of a node group.

    @param uuid: Node group UUID
    @param primary_only: Whether to only consider primary nodes
    @rtype: frozenset
    @return: List of instance UUIDs in node group

    """
    if primary_only:
      nodes_fn = lambda inst: [inst.primary_node]
    else:
      nodes_fn = lambda inst: self._UnlockedGetInstanceNodes(inst.uuid)

    return frozenset(inst.uuid
                     for inst in self._ConfigData().instances.values()
                     for node_uuid in nodes_fn(inst)
                     if self._UnlockedGetNodeInfo(node_uuid).group == uuid)

  def _UnlockedGetHvparamsString(self, hvname):
    """Return the string representation of the list of hyervisor parameters of
    the given hypervisor.

    @see: C{GetHvparams}

    """
    result = ""
    hvparams = self._ConfigData().cluster.hvparams[hvname]
    for key in hvparams:
      result += "%s=%s\n" % (key, hvparams[key])
    return result

  @_ConfigSync(shared=1)
  def GetHvparamsString(self, hvname):
    """Return the hypervisor parameters of the given hypervisor.

    @type hvname: string
    @param hvname: name of a hypervisor
    @rtype: string
    @return: string containing key-value-pairs, one pair on each line;
      format: KEY=VALUE

    """
    return self._UnlockedGetHvparamsString(hvname)

  def _UnlockedGetNodeList(self):
    """Return the list of nodes which are in the configuration.

    This function is for internal use, when the config lock is already
    held.

    @rtype: list

    """
    return self._ConfigData().nodes.keys()

  @_ConfigSync(shared=1)
  def GetNodeList(self):
    """Return the list of nodes which are in the configuration.

    """
    return self._UnlockedGetNodeList()

  def _UnlockedGetOnlineNodeList(self):
    """Return the list of nodes which are online.

    """
    all_nodes = [self._UnlockedGetNodeInfo(node)
                 for node in self._UnlockedGetNodeList()]
    return [node.uuid for node in all_nodes if not node.offline]

  @_ConfigSync(shared=1)
  def GetOnlineNodeList(self):
    """Return the list of nodes which are online.

    """
    return self._UnlockedGetOnlineNodeList()

  @_ConfigSync(shared=1)
  def GetVmCapableNodeList(self):
    """Return the list of nodes which are not vm capable.

    """
    all_nodes = [self._UnlockedGetNodeInfo(node)
                 for node in self._UnlockedGetNodeList()]
    return [node.uuid for node in all_nodes if node.vm_capable]

  @_ConfigSync(shared=1)
  def GetNonVmCapableNodeList(self):
    """Return the list of nodes' uuids which are not vm capable.

    """
    all_nodes = [self._UnlockedGetNodeInfo(node)
                 for node in self._UnlockedGetNodeList()]
    return [node.uuid for node in all_nodes if not node.vm_capable]

  @_ConfigSync(shared=1)
  def GetNonVmCapableNodeNameList(self):
    """Return the list of nodes' names which are not vm capable.

    """
    all_nodes = [self._UnlockedGetNodeInfo(node)
                 for node in self._UnlockedGetNodeList()]
    return [node.name for node in all_nodes if not node.vm_capable]

  @_ConfigSync(shared=1)
  def GetMultiNodeInfo(self, node_uuids):
    """Get the configuration of multiple nodes.

    @param node_uuids: list of node UUIDs
    @rtype: list
    @return: list of tuples of (node, node_info), where node_info is
        what would GetNodeInfo return for the node, in the original
        order

    """
    return [(uuid, self._UnlockedGetNodeInfo(uuid)) for uuid in node_uuids]

  def _UnlockedGetAllNodesInfo(self):
    """Gets configuration of all nodes.

    @note: See L{GetAllNodesInfo}

    """
    return dict([(node_uuid, self._UnlockedGetNodeInfo(node_uuid))
                 for node_uuid in self._UnlockedGetNodeList()])

  @_ConfigSync(shared=1)
  def GetAllNodesInfo(self):
    """Get the configuration of all nodes.

    @rtype: dict
    @return: dict of (node, node_info), where node_info is what
              would GetNodeInfo return for the node

    """
    return self._UnlockedGetAllNodesInfo()

  def _UnlockedGetNodeInfoByName(self, node_name):
    for node in self._UnlockedGetAllNodesInfo().values():
      if node.name == node_name:
        return node
    return None

  @_ConfigSync(shared=1)
  def GetNodeInfoByName(self, node_name):
    """Get the L{objects.Node} object for a named node.

    @param node_name: name of the node to get information for
    @type node_name: string
    @return: the corresponding L{objects.Node} instance or None if no
          information is available

    """
    return self._UnlockedGetNodeInfoByName(node_name)

  @_ConfigSync(shared=1)
  def GetNodeGroupInfoByName(self, nodegroup_name):
    """Get the L{objects.NodeGroup} object for a named node group.

    @param nodegroup_name: name of the node group to get information for
    @type nodegroup_name: string
    @return: the corresponding L{objects.NodeGroup} instance or None if no
          information is available

    """
    for nodegroup in self._UnlockedGetAllNodeGroupsInfo().values():
      if nodegroup.name == nodegroup_name:
        return nodegroup
    return None

  def _UnlockedGetNodeName(self, node_spec):
    if isinstance(node_spec, objects.Node):
      return node_spec.name
    elif isinstance(node_spec, basestring):
      node_info = self._UnlockedGetNodeInfo(node_spec)
      if node_info is None:
        raise errors.OpExecError("Unknown node: %s" % node_spec)
      return node_info.name
    else:
      raise errors.ProgrammerError("Can't handle node spec '%s'" % node_spec)

  @_ConfigSync(shared=1)
  def GetNodeName(self, node_spec):
    """Gets the node name for the passed node.

    @param node_spec: node to get names for
    @type node_spec: either node UUID or a L{objects.Node} object
    @rtype: string
    @return: node name

    """
    return self._UnlockedGetNodeName(node_spec)

  def _UnlockedGetNodeNames(self, node_specs):
    return [self._UnlockedGetNodeName(node_spec) for node_spec in node_specs]

  @_ConfigSync(shared=1)
  def GetNodeNames(self, node_specs):
    """Gets the node names for the passed list of nodes.

    @param node_specs: list of nodes to get names for
    @type node_specs: list of either node UUIDs or L{objects.Node} objects
    @rtype: list of strings
    @return: list of node names

    """
    return self._UnlockedGetNodeNames(node_specs)

  @_ConfigSync(shared=1)
  def GetNodeGroupsFromNodes(self, node_uuids):
    """Returns groups for a list of nodes.

    @type node_uuids: list of string
    @param node_uuids: List of node UUIDs
    @rtype: frozenset

    """
    return frozenset(self._UnlockedGetNodeInfo(uuid).group
                     for uuid in node_uuids)

  def _UnlockedGetMasterCandidateUuids(self):
    """Get the list of UUIDs of master candidates.

    @rtype: list of strings
    @return: list of UUIDs of all master candidates.

    """
    return [node.uuid for node in self._ConfigData().nodes.values()
            if node.master_candidate]

  @_ConfigSync(shared=1)
  def GetMasterCandidateUuids(self):
    """Get the list of UUIDs of master candidates.

    @rtype: list of strings
    @return: list of UUIDs of all master candidates.

    """
    return self._UnlockedGetMasterCandidateUuids()

  def _UnlockedGetMasterCandidateStats(self, exceptions=None):
    """Get the number of current and maximum desired and possible candidates.

    @type exceptions: list
    @param exceptions: if passed, list of nodes that should be ignored
    @rtype: tuple
    @return: tuple of (current, desired and possible, possible)

    """
    mc_now = mc_should = mc_max = 0
    for node in self._ConfigData().nodes.values():
      if exceptions and node.uuid in exceptions:
        continue
      if not (node.offline or node.drained) and node.master_capable:
        mc_max += 1
      if node.master_candidate:
        mc_now += 1
    mc_should = min(mc_max, self._ConfigData().cluster.candidate_pool_size)
    return (mc_now, mc_should, mc_max)

  @_ConfigSync(shared=1)
  def GetMasterCandidateStats(self, exceptions=None):
    """Get the number of current and maximum possible candidates.

    This is just a wrapper over L{_UnlockedGetMasterCandidateStats}.

    @type exceptions: list
    @param exceptions: if passed, list of nodes that should be ignored
    @rtype: tuple
    @return: tuple of (current, max)

    """
    return self._UnlockedGetMasterCandidateStats(exceptions)

  @_ConfigSync()
  def MaintainCandidatePool(self, exception_node_uuids):
    """Try to grow the candidate pool to the desired size.

    @type exception_node_uuids: list
    @param exception_node_uuids: if passed, list of nodes that should be ignored
    @rtype: list
    @return: list with the adjusted nodes (L{objects.Node} instances)

    """
    mc_now, mc_max, _ = self._UnlockedGetMasterCandidateStats(
                          exception_node_uuids)
    mod_list = []
    if mc_now < mc_max:
      node_list = self._ConfigData().nodes.keys()
      random.shuffle(node_list)
      for uuid in node_list:
        if mc_now >= mc_max:
          break
        node = self._ConfigData().nodes[uuid]
        if (node.master_candidate or node.offline or node.drained or
            node.uuid in exception_node_uuids or not node.master_capable):
          continue
        mod_list.append(node)
        node.master_candidate = True
        node.serial_no += 1
        mc_now += 1
      if mc_now != mc_max:
        # this should not happen
        logging.warning("Warning: MaintainCandidatePool didn't manage to"
                        " fill the candidate pool (%d/%d)", mc_now, mc_max)
      if mod_list:
        self._ConfigData().cluster.serial_no += 1

    return mod_list

  def _UnlockedAddNodeToGroup(self, node_uuid, nodegroup_uuid):
    """Add a given node to the specified group.

    """
    if nodegroup_uuid not in self._ConfigData().nodegroups:
      # This can happen if a node group gets deleted between its lookup and
      # when we're adding the first node to it, since we don't keep a lock in
      # the meantime. It's ok though, as we'll fail cleanly if the node group
      # is not found anymore.
      raise errors.OpExecError("Unknown node group: %s" % nodegroup_uuid)
    if node_uuid not in self._ConfigData().nodegroups[nodegroup_uuid].members:
      self._ConfigData().nodegroups[nodegroup_uuid].members.append(node_uuid)

  def _UnlockedRemoveNodeFromGroup(self, node):
    """Remove a given node from its group.

    """
    nodegroup = node.group
    if nodegroup not in self._ConfigData().nodegroups:
      logging.warning("Warning: node '%s' has unknown node group '%s'"
                      " (while being removed from it)", node.uuid, nodegroup)
    nodegroup_obj = self._ConfigData().nodegroups[nodegroup]
    if node.uuid not in nodegroup_obj.members:
      logging.warning("Warning: node '%s' not a member of its node group '%s'"
                      " (while being removed from it)", node.uuid, nodegroup)
    else:
      nodegroup_obj.members.remove(node.uuid)

  @_ConfigSync()
  def AssignGroupNodes(self, mods):
    """Changes the group of a number of nodes.

    @type mods: list of tuples; (node name, new group UUID)
    @param mods: Node membership modifications

    """
    groups = self._ConfigData().nodegroups
    nodes = self._ConfigData().nodes

    resmod = []

    # Try to resolve UUIDs first
    for (node_uuid, new_group_uuid) in mods:
      try:
        node = nodes[node_uuid]
      except KeyError:
        raise errors.ConfigurationError("Unable to find node '%s'" % node_uuid)

      if node.group == new_group_uuid:
        # Node is being assigned to its current group
        logging.debug("Node '%s' was assigned to its current group (%s)",
                      node_uuid, node.group)
        continue

      # Try to find current group of node
      try:
        old_group = groups[node.group]
      except KeyError:
        raise errors.ConfigurationError("Unable to find old group '%s'" %
                                        node.group)

      # Try to find new group for node
      try:
        new_group = groups[new_group_uuid]
      except KeyError:
        raise errors.ConfigurationError("Unable to find new group '%s'" %
                                        new_group_uuid)

      assert node.uuid in old_group.members, \
        ("Inconsistent configuration: node '%s' not listed in members for its"
         " old group '%s'" % (node.uuid, old_group.uuid))
      assert node.uuid not in new_group.members, \
        ("Inconsistent configuration: node '%s' already listed in members for"
         " its new group '%s'" % (node.uuid, new_group.uuid))

      resmod.append((node, old_group, new_group))

    # Apply changes
    for (node, old_group, new_group) in resmod:
      assert node.uuid != new_group.uuid and old_group.uuid != new_group.uuid, \
        "Assigning to current group is not possible"

      node.group = new_group.uuid

      # Update members of involved groups
      if node.uuid in old_group.members:
        old_group.members.remove(node.uuid)
      if node.uuid not in new_group.members:
        new_group.members.append(node.uuid)

    # Update timestamps and serials (only once per node/group object)
    now = time.time()
    for obj in frozenset(itertools.chain(*resmod)): # pylint: disable=W0142
      obj.serial_no += 1
      obj.mtime = now

    # Force ssconf update
    self._ConfigData().cluster.serial_no += 1

  def _BumpSerialNo(self):
    """Bump up the serial number of the config.

    """
    self._ConfigData().serial_no += 1
    self._ConfigData().mtime = time.time()

  def _AllUUIDObjects(self):
    """Returns all objects with uuid attributes.

    """
    return (self._ConfigData().instances.values() +
            self._ConfigData().nodes.values() +
            self._ConfigData().nodegroups.values() +
            self._ConfigData().networks.values() +
            self._ConfigData().disks.values() +
            self._AllNICs() +
            [self._ConfigData().cluster])

  def GetConfigManager(self, shared=False, forcelock=False):
    """Returns a ConfigManager, which is suitable to perform a synchronized
    block of configuration operations.

    WARNING: This blocks all other configuration operations, so anything that
    runs inside the block should be very fast, preferably not using any IO.
    """

    return ConfigManager(self, shared=shared, forcelock=forcelock)

  def _AddLockCount(self, count):
    self._lock_count += count
    return self._lock_count

  def _LockCount(self):
    return self._lock_count

  def _OpenConfig(self, shared, force=False):
    """Read the config data from WConfd or disk.

    """
    if self._AddLockCount(1) > 1:
      if self._lock_current_shared and not shared:
        self._AddLockCount(-1)
        raise errors.ConfigurationError("Can't request an exclusive"
                                        " configuration lock while holding"
                                        " shared")
      elif not force or self._lock_forced or not shared or self._offline:
        return # we already have the lock, do nothing
    else:
      self._lock_current_shared = shared
    if force:
      self._lock_forced = True
    # Read the configuration data. If offline, read the file directly.
    # If online, call WConfd.
    if self._offline:
      try:
        raw_data = utils.ReadFile(self._cfg_file)
        data_dict = serializer.Load(raw_data)
        # Make sure the configuration has the right version
        _ValidateConfig(data_dict)
        data = objects.ConfigData.FromDict(data_dict)
      except errors.ConfigVersionMismatch:
        raise
      except Exception, err:
        raise errors.ConfigurationError(err)

      self._cfg_id = utils.GetFileID(path=self._cfg_file)

      if (not hasattr(data, "cluster") or
          not hasattr(data.cluster, "rsahostkeypub")):
        raise errors.ConfigurationError("Incomplete configuration"
                                        " (missing cluster.rsahostkeypub)")

      if not data.cluster.master_node in data.nodes:
        msg = ("The configuration denotes node %s as master, but does not"
               " contain information about this node" %
               data.cluster.master_node)
        raise errors.ConfigurationError(msg)

      master_info = data.nodes[data.cluster.master_node]
      if master_info.name != self._my_hostname and not self._accept_foreign:
        msg = ("The configuration denotes node %s as master, while my"
               " hostname is %s; opening a foreign configuration is only"
               " possible in accept_foreign mode" %
               (master_info.name, self._my_hostname))
        raise errors.ConfigurationError(msg)

      self._SetConfigData(data)

      # Upgrade configuration if needed
      self._UpgradeConfig(saveafter=True)
    else:
      if shared and not force:
        if self._config_data is None:
          logging.debug("Requesting config, as I have no up-to-date copy")
          dict_data = self._wconfd.ReadConfig()
        else:
          logging.debug("My config copy is up to date.")
          dict_data = None
      else:
        # poll until we acquire the lock
        while True:
          dict_data = \
              self._wconfd.LockConfig(self._GetWConfdContext(), bool(shared))
          logging.debug("Received config from WConfd.LockConfig [shared=%s]",
                        bool(shared))
          if dict_data is not None:
            break
          time.sleep(random.random())

      try:
        if dict_data is not None:
          self._SetConfigData(objects.ConfigData.FromDict(dict_data))
      except Exception, err:
        raise errors.ConfigurationError(err)

      # Transitional fix until ConfigWriter is completely rewritten into
      # Haskell
      self._UpgradeConfig()

  def _CloseConfig(self, save):
    """Release resources relating the config data.

    """
    if self._AddLockCount(-1) > 0:
      return # we still have the lock, do nothing
    if save:
      try:
        logging.debug("Writing configuration and unlocking it")
        self._WriteConfig(releaselock=True)
      except Exception, err:
        logging.critical("Can't write the configuration: %s", str(err))
        raise
    elif not self._offline and \
         not (self._lock_current_shared and not self._lock_forced):
      logging.debug("Unlocking configuration without writing")
      self._wconfd.UnlockConfig(self._GetWConfdContext())
      self._lock_forced = False

  # TODO: To WConfd
  def _UpgradeConfig(self, saveafter=False):
    """Run any upgrade steps.

    This method performs both in-object upgrades and also update some data
    elements that need uniqueness across the whole configuration or interact
    with other objects.

    @warning: if 'saveafter' is 'True', this function will call
        L{_WriteConfig()} so it needs to be called only from a
        "safe" place.

    """
    # Keep a copy of the persistent part of _config_data to check for changes
    # Serialization doesn't guarantee order in dictionaries
    oldconf = copy.deepcopy(self._ConfigData().ToDict())

    # In-object upgrades
    self._ConfigData().UpgradeConfig()

    for item in self._AllUUIDObjects():
      if item.uuid is None:
        item.uuid = self._GenerateUniqueID(_UPGRADE_CONFIG_JID)
    if not self._ConfigData().nodegroups:
      default_nodegroup_name = constants.INITIAL_NODE_GROUP_NAME
      default_nodegroup = objects.NodeGroup(name=default_nodegroup_name,
                                            members=[])
      self._UnlockedAddNodeGroup(default_nodegroup, _UPGRADE_CONFIG_JID, True)
    for node in self._ConfigData().nodes.values():
      if not node.group:
        node.group = self._UnlockedLookupNodeGroup(None)
      # This is technically *not* an upgrade, but needs to be done both when
      # nodegroups are being added, and upon normally loading the config,
      # because the members list of a node group is discarded upon
      # serializing/deserializing the object.
      self._UnlockedAddNodeToGroup(node.uuid, node.group)

    modified = (oldconf != self._ConfigData().ToDict())
    if modified and saveafter:
      self._WriteConfig()
      self._UnlockedDropECReservations(_UPGRADE_CONFIG_JID)
    else:
      if self._offline:
        self._UnlockedVerifyConfigAndLog()

  def _WriteConfig(self, destination=None, releaselock=False):
    """Write the configuration data to persistent storage.

    """
    if destination is None:
      destination = self._cfg_file

    # Save the configuration data. If offline, write the file directly.
    # If online, call WConfd.
    if self._offline:
      self._BumpSerialNo()
      txt = serializer.DumpJson(
        self._ConfigData().ToDict(_with_private=True),
        private_encoder=serializer.EncodeWithPrivateFields
      )

      getents = self._getents()
      try:
        fd = utils.SafeWriteFile(destination, self._cfg_id, data=txt,
                                 close=False, gid=getents.confd_gid, mode=0640)
      except errors.LockError:
        raise errors.ConfigurationError("The configuration file has been"
                                        " modified since the last write, cannot"
                                        " update")
      try:
        self._cfg_id = utils.GetFileID(fd=fd)
      finally:
        os.close(fd)
    else:
      try:
        if releaselock:
          self._wconfd.WriteConfigAndUnlock(self._GetWConfdContext(),
                                            self._ConfigData().ToDict())
        else:
          self._wconfd.WriteConfig(self._GetWConfdContext(),
                                   self._ConfigData().ToDict())
      except errors.LockError:
        raise errors.ConfigurationError("The configuration file has been"
                                        " modified since the last write, cannot"
                                        " update")

    self.write_count += 1

  def _GetAllHvparamsStrings(self, hypervisors):
    """Get the hvparams of all given hypervisors from the config.

    @type hypervisors: list of string
    @param hypervisors: list of hypervisor names
    @rtype: dict of strings
    @returns: dictionary mapping the hypervisor name to a string representation
      of the hypervisor's hvparams

    """
    hvparams = {}
    for hv in hypervisors:
      hvparams[hv] = self._UnlockedGetHvparamsString(hv)
    return hvparams

  @staticmethod
  def _ExtendByAllHvparamsStrings(ssconf_values, all_hvparams):
    """Extends the ssconf_values dictionary by hvparams.

    @type ssconf_values: dict of strings
    @param ssconf_values: dictionary mapping ssconf_keys to strings
      representing the content of ssconf files
    @type all_hvparams: dict of strings
    @param all_hvparams: dictionary mapping hypervisor names to a string
      representation of their hvparams
    @rtype: same as ssconf_values
    @returns: the ssconf_values dictionary extended by hvparams

    """
    for hv in all_hvparams:
      ssconf_key = constants.SS_HVPARAMS_PREF + hv
      ssconf_values[ssconf_key] = all_hvparams[hv]
    return ssconf_values

  def _UnlockedGetSsconfValues(self):
    """Return the values needed by ssconf.

    @rtype: dict
    @return: a dictionary with keys the ssconf names and values their
        associated value

    """
    fn = "\n".join
    instance_names = utils.NiceSort(
                       [inst.name for inst in
                        self._UnlockedGetAllInstancesInfo().values()])
    node_infos = self._UnlockedGetAllNodesInfo().values()
    node_names = [node.name for node in node_infos]
    node_pri_ips = ["%s %s" % (ninfo.name, ninfo.primary_ip)
                    for ninfo in node_infos]
    node_snd_ips = ["%s %s" % (ninfo.name, ninfo.secondary_ip)
                    for ninfo in node_infos]
    node_vm_capable = ["%s=%s" % (ninfo.name, str(ninfo.vm_capable))
                       for ninfo in node_infos]

    instance_data = fn(instance_names)
    off_data = fn(node.name for node in node_infos if node.offline)
    on_data = fn(node.name for node in node_infos if not node.offline)
    mc_data = fn(node.name for node in node_infos if node.master_candidate)
    mc_ips_data = fn(node.primary_ip for node in node_infos
                     if node.master_candidate)
    node_data = fn(node_names)
    node_pri_ips_data = fn(node_pri_ips)
    node_snd_ips_data = fn(node_snd_ips)
    node_vm_capable_data = fn(node_vm_capable)

    cluster = self._ConfigData().cluster
    cluster_tags = fn(cluster.GetTags())

    master_candidates_certs = fn("%s=%s" % (mc_uuid, mc_cert)
                                 for mc_uuid, mc_cert
                                 in cluster.candidate_certs.items())

    hypervisor_list = fn(cluster.enabled_hypervisors)
    all_hvparams = self._GetAllHvparamsStrings(constants.HYPER_TYPES)

    uid_pool = uidpool.FormatUidPool(cluster.uid_pool, separator="\n")

    nodegroups = ["%s %s" % (nodegroup.uuid, nodegroup.name) for nodegroup in
                  self._ConfigData().nodegroups.values()]
    nodegroups_data = fn(utils.NiceSort(nodegroups))
    networks = ["%s %s" % (net.uuid, net.name) for net in
                self._ConfigData().networks.values()]
    networks_data = fn(utils.NiceSort(networks))

    ssconf_values = {
      constants.SS_CLUSTER_NAME: cluster.cluster_name,
      constants.SS_CLUSTER_TAGS: cluster_tags,
      constants.SS_FILE_STORAGE_DIR: cluster.file_storage_dir,
      constants.SS_SHARED_FILE_STORAGE_DIR: cluster.shared_file_storage_dir,
      constants.SS_GLUSTER_STORAGE_DIR: cluster.gluster_storage_dir,
      constants.SS_MASTER_CANDIDATES: mc_data,
      constants.SS_MASTER_CANDIDATES_IPS: mc_ips_data,
      constants.SS_MASTER_CANDIDATES_CERTS: master_candidates_certs,
      constants.SS_MASTER_IP: cluster.master_ip,
      constants.SS_MASTER_NETDEV: cluster.master_netdev,
      constants.SS_MASTER_NETMASK: str(cluster.master_netmask),
      constants.SS_MASTER_NODE: self._UnlockedGetNodeName(cluster.master_node),
      constants.SS_NODE_LIST: node_data,
      constants.SS_NODE_PRIMARY_IPS: node_pri_ips_data,
      constants.SS_NODE_SECONDARY_IPS: node_snd_ips_data,
      constants.SS_NODE_VM_CAPABLE: node_vm_capable_data,
      constants.SS_OFFLINE_NODES: off_data,
      constants.SS_ONLINE_NODES: on_data,
      constants.SS_PRIMARY_IP_FAMILY: str(cluster.primary_ip_family),
      constants.SS_INSTANCE_LIST: instance_data,
      constants.SS_RELEASE_VERSION: constants.RELEASE_VERSION,
      constants.SS_HYPERVISOR_LIST: hypervisor_list,
      constants.SS_MAINTAIN_NODE_HEALTH: str(cluster.maintain_node_health),
      constants.SS_UID_POOL: uid_pool,
      constants.SS_NODEGROUPS: nodegroups_data,
      constants.SS_NETWORKS: networks_data,
      constants.SS_ENABLED_USER_SHUTDOWN: str(cluster.enabled_user_shutdown),
      }
    ssconf_values = self._ExtendByAllHvparamsStrings(ssconf_values,
                                                     all_hvparams)
    bad_values = [(k, v) for k, v in ssconf_values.items()
                  if not isinstance(v, (str, basestring))]
    if bad_values:
      err = utils.CommaJoin("%s=%s" % (k, v) for k, v in bad_values)
      raise errors.ConfigurationError("Some ssconf key(s) have non-string"
                                      " values: %s" % err)
    return ssconf_values

  @_ConfigSync(shared=1)
  def GetSsconfValues(self):
    """Wrapper using lock around _UnlockedGetSsconf().

    """
    return self._UnlockedGetSsconfValues()

  @_ConfigSync(shared=1)
  def GetVGName(self):
    """Return the volume group name.

    """
    return self._ConfigData().cluster.volume_group_name

  @_ConfigSync()
  def SetVGName(self, vg_name):
    """Set the volume group name.

    """
    self._ConfigData().cluster.volume_group_name = vg_name
    self._ConfigData().cluster.serial_no += 1

  @_ConfigSync(shared=1)
  def GetDRBDHelper(self):
    """Return DRBD usermode helper.

    """
    return self._ConfigData().cluster.drbd_usermode_helper

  @_ConfigSync()
  def SetDRBDHelper(self, drbd_helper):
    """Set DRBD usermode helper.

    """
    self._ConfigData().cluster.drbd_usermode_helper = drbd_helper
    self._ConfigData().cluster.serial_no += 1

  @_ConfigSync(shared=1)
  def GetMACPrefix(self):
    """Return the mac prefix.

    """
    return self._ConfigData().cluster.mac_prefix

  @_ConfigSync(shared=1)
  def GetClusterInfo(self):
    """Returns information about the cluster

    @rtype: L{objects.Cluster}
    @return: the cluster object

    """
    return self._ConfigData().cluster

  @_ConfigSync(shared=1)
  def HasAnyDiskOfType(self, dev_type):
    """Check if in there is at disk of the given type in the configuration.

    """
    return self._ConfigData().HasAnyDiskOfType(dev_type)

  @_ConfigSync(shared=1)
  def GetDetachedConfig(self):
    """Returns a detached version of a ConfigManager, which represents
    a read-only snapshot of the configuration at this particular time.

    """
    return DetachedConfig(self._ConfigData())

  @_ConfigSync()
  def Update(self, target, feedback_fn, ec_id=None):
    """Notify function to be called after updates.

    This function must be called when an object (as returned by
    GetInstanceInfo, GetNodeInfo, GetCluster) has been updated and the
    caller wants the modifications saved to the backing store. Note
    that all modified objects will be saved, but the target argument
    is the one the caller wants to ensure that it's saved.

    @param target: an instance of either L{objects.Cluster},
        L{objects.Node} or L{objects.Instance} which is existing in
        the cluster
    @param feedback_fn: Callable feedback function

    """
    if self._ConfigData() is None:
      raise errors.ProgrammerError("Configuration file not read,"
                                   " cannot save.")

    def check_serial(target, current):
      if current is None:
        raise errors.ConfigurationError("Configuration object unknown")
      elif current.serial_no != target.serial_no:
        raise errors.ConfigurationError("Configuration object updated since"
                                        " it has been read: %d != %d",
                                        current.serial_no, target.serial_no)

    def replace_in(target, tdict):
      check_serial(target, tdict.get(target.uuid))
      tdict[target.uuid] = target

    update_serial = False
    if isinstance(target, objects.Cluster):
      check_serial(target, self._ConfigData().cluster)
      self._ConfigData().cluster = target
    elif isinstance(target, objects.Node):
      replace_in(target, self._ConfigData().nodes)
      update_serial = True
    elif isinstance(target, objects.Instance):
      replace_in(target, self._ConfigData().instances)
    elif isinstance(target, objects.NodeGroup):
      replace_in(target, self._ConfigData().nodegroups)
    elif isinstance(target, objects.Network):
      replace_in(target, self._ConfigData().networks)
    elif isinstance(target, objects.Disk):
      replace_in(target, self._ConfigData().disks)
    else:
      raise errors.ProgrammerError("Invalid object type (%s) passed to"
                                   " ConfigWriter.Update" % type(target))
    target.serial_no += 1
    target.mtime = now = time.time()

    if update_serial:
      # for node updates, we need to increase the cluster serial too
      self._ConfigData().cluster.serial_no += 1
      self._ConfigData().cluster.mtime = now

    if isinstance(target, objects.Instance):
      self._UnlockedReleaseDRBDMinors(target.uuid)

    if ec_id is not None:
      # Commit all ips reserved by OpInstanceSetParams and OpGroupSetParams
      # FIXME: After RemoveInstance is moved to WConfd, use its internal
      # functions from TempRes module.
      self._UnlockedCommitTemporaryIps(ec_id)

    # Just verify the configuration with our feedback function.
    # It will get written automatically by the decorator.
    self._UnlockedVerifyConfigAndLog(feedback_fn=feedback_fn)

  def _UnlockedDropECReservations(self, _ec_id):
    """Drop per-execution-context reservations

    """
    # FIXME: Remove the following two lines after all reservations are moved to
    # wconfd.
    for rm in self._all_rms:
      rm.DropECReservations(_ec_id)
    if not self._offline:
      self._wconfd.DropAllReservations(self._GetWConfdContext())

  def DropECReservations(self, ec_id):
    self._UnlockedDropECReservations(ec_id)

  @_ConfigSync(shared=1)
  def GetAllNetworksInfo(self):
    """Get configuration info of all the networks.

    """
    return dict(self._ConfigData().networks)

  def _UnlockedGetNetworkList(self):
    """Get the list of networks.

    This function is for internal use, when the config lock is already held.

    """
    return self._ConfigData().networks.keys()

  @_ConfigSync(shared=1)
  def GetNetworkList(self):
    """Get the list of networks.

    @return: array of networks, ex. ["main", "vlan100", "200]

    """
    return self._UnlockedGetNetworkList()

  @_ConfigSync(shared=1)
  def GetNetworkNames(self):
    """Get a list of network names

    """
    names = [net.name
             for net in self._ConfigData().networks.values()]
    return names

  def _UnlockedGetNetwork(self, uuid):
    """Returns information about a network.

    This function is for internal use, when the config lock is already held.

    """
    if uuid not in self._ConfigData().networks:
      return None

    return self._ConfigData().networks[uuid]

  @_ConfigSync(shared=1)
  def GetNetwork(self, uuid):
    """Returns information about a network.

    It takes the information from the configuration file.

    @param uuid: UUID of the network

    @rtype: L{objects.Network}
    @return: the network object

    """
    return self._UnlockedGetNetwork(uuid)

  @_ConfigSync()
  def AddNetwork(self, net, ec_id, check_uuid=True):
    """Add a network to the configuration.

    @type net: L{objects.Network}
    @param net: the Network object to add
    @type ec_id: string
    @param ec_id: unique id for the job to use when creating a missing UUID

    """
    self._UnlockedAddNetwork(net, ec_id, check_uuid)

  def _UnlockedAddNetwork(self, net, ec_id, check_uuid):
    """Add a network to the configuration.

    """
    logging.info("Adding network %s to configuration", net.name)

    if check_uuid:
      self._EnsureUUID(net, ec_id)

    net.serial_no = 1
    net.ctime = net.mtime = time.time()
    self._ConfigData().networks[net.uuid] = net
    self._ConfigData().cluster.serial_no += 1

  def _UnlockedLookupNetwork(self, target):
    """Lookup a network's UUID.

    @type target: string
    @param target: network name or UUID
    @rtype: string
    @return: network UUID
    @raises errors.OpPrereqError: when the target network cannot be found

    """
    if target is None:
      return None
    if target in self._ConfigData().networks:
      return target
    for net in self._ConfigData().networks.values():
      if net.name == target:
        return net.uuid
    raise errors.OpPrereqError("Network '%s' not found" % target,
                               errors.ECODE_NOENT)

  @_ConfigSync(shared=1)
  def LookupNetwork(self, target):
    """Lookup a network's UUID.

    This function is just a wrapper over L{_UnlockedLookupNetwork}.

    @type target: string
    @param target: network name or UUID
    @rtype: string
    @return: network UUID

    """
    return self._UnlockedLookupNetwork(target)

  @_ConfigSync()
  def RemoveNetwork(self, network_uuid):
    """Remove a network from the configuration.

    @type network_uuid: string
    @param network_uuid: the UUID of the network to remove

    """
    logging.info("Removing network %s from configuration", network_uuid)

    if network_uuid not in self._ConfigData().networks:
      raise errors.ConfigurationError("Unknown network '%s'" % network_uuid)

    del self._ConfigData().networks[network_uuid]
    self._ConfigData().cluster.serial_no += 1

  def _UnlockedGetGroupNetParams(self, net_uuid, node_uuid):
    """Get the netparams (mode, link) of a network.

    Get a network's netparams for a given node.

    @type net_uuid: string
    @param net_uuid: network uuid
    @type node_uuid: string
    @param node_uuid: node UUID
    @rtype: dict or None
    @return: netparams

    """
    node_info = self._UnlockedGetNodeInfo(node_uuid)
    nodegroup_info = self._UnlockedGetNodeGroup(node_info.group)
    netparams = nodegroup_info.networks.get(net_uuid, None)

    return netparams

  @_ConfigSync(shared=1)
  def GetGroupNetParams(self, net_uuid, node_uuid):
    """Locking wrapper of _UnlockedGetGroupNetParams()

    """
    return self._UnlockedGetGroupNetParams(net_uuid, node_uuid)

  @_ConfigSync(shared=1)
  def CheckIPInNodeGroup(self, ip, node_uuid):
    """Check IP uniqueness in nodegroup.

    Check networks that are connected in the node's node group
    if ip is contained in any of them. Used when creating/adding
    a NIC to ensure uniqueness among nodegroups.

    @type ip: string
    @param ip: ip address
    @type node_uuid: string
    @param node_uuid: node UUID
    @rtype: (string, dict) or (None, None)
    @return: (network name, netparams)

    """
    if ip is None:
      return (None, None)
    node_info = self._UnlockedGetNodeInfo(node_uuid)
    nodegroup_info = self._UnlockedGetNodeGroup(node_info.group)
    for net_uuid in nodegroup_info.networks.keys():
      net_info = self._UnlockedGetNetwork(net_uuid)
      pool = network.AddressPool(net_info)
      if pool.Contains(ip):
        return (net_info.name, nodegroup_info.networks[net_uuid])

    return (None, None)

  @_ConfigSync(shared=1)
  def GetCandidateCerts(self):
    """Returns the candidate certificate map.

    """
    return self._ConfigData().cluster.candidate_certs

  @_ConfigSync()
  def AddNodeToCandidateCerts(self, node_uuid, cert_digest,
                              info_fn=logging.info, warn_fn=logging.warn):
    """Adds an entry to the candidate certificate map.

    @type node_uuid: string
    @param node_uuid: the node's UUID
    @type cert_digest: string
    @param cert_digest: the digest of the node's client SSL certificate
    @type info_fn: function
    @param info_fn: logging function for information messages
    @type warn_fn: function
    @param warn_fn: logging function for warning messages

    """
    cluster = self._ConfigData().cluster
    if node_uuid in cluster.candidate_certs:
      old_cert_digest = cluster.candidate_certs[node_uuid]
      if old_cert_digest == cert_digest:
        if info_fn is not None:
          info_fn("Certificate digest for node %s already in config."
                  "Not doing anything." % node_uuid)
        return
      else:
        if warn_fn is not None:
          warn_fn("Overriding differing certificate digest for node %s"
                  % node_uuid)
    cluster.candidate_certs[node_uuid] = cert_digest

  @_ConfigSync()
  def RemoveNodeFromCandidateCerts(self, node_uuid,
                                   warn_fn=logging.warn):
    """Removes the entry of the given node in the certificate map.

    @type node_uuid: string
    @param node_uuid: the node's UUID
    @type warn_fn: function
    @param warn_fn: logging function for warning messages

    """
    cluster = self._ConfigData().cluster
    if node_uuid not in cluster.candidate_certs:
      if warn_fn is not None:
        warn_fn("Cannot remove certifcate for node %s, because it's not"
                " in the candidate map." % node_uuid)
      return
    del cluster.candidate_certs[node_uuid]

  def FlushConfig(self):
    """Force the distribution of configuration to master candidates.

    It is not necessary to hold a lock for this operation, it is handled
    internally by WConfd.

    """
    if not self._offline:
      self._wconfd.FlushConfig()


class DetachedConfig(ConfigWriter):
  def __init__(self, config_data):
    super(DetachedConfig, self).__init__(self, offline=True)
    self._SetConfigData(config_data)

  @staticmethod
  def _WriteCallError():
    raise errors.ProgrammerError("DetachedConfig supports only read-only"
                                 " operations")

  def _OpenConfig(self, shared, force=None):
    if not shared:
      DetachedConfig._WriteCallError()

  def _CloseConfig(self, save):
    if save:
      DetachedConfig._WriteCallError()
