#!/usr/bin/env python
#
# mysql-ebs-snapshot v1.0
# Copyright 2015 Ivgeni Segal
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os, sys, subprocess
import time, datetime
import MySQLdb
import boto.ec2
import boto.utils
import logging
import re
import functools
import time

##############################
#  BEGIN - settings section  #
##############################

# AWS Credentials
AWS_KEY = '<< PASTE YOUR AWS KEY HERE >>'
AWS_SECRET_KEY = '<< PASTE YOUR AWS SECRET KEY HERE >>'

# Mysql data directory
MYSQL_DATA_DIR = '/var/lib/mysql'

# Mysql host, username and password
# used for FLUSH commands
MYSQL_HOST='localhost'
MYSQL_USERNAME='backup'
MYSQL_PASSWORD='<< PASTE THE PASSWORD FOR BACKUP USER >>'

# For testing - disable FS freeze or actual snapshot
# creation
NO_FS_FREEZE = False
NO_SNAPSHOT = False

XFS_FREEZE_BIN = '/usr/sbin/xfs_freeze'

# Number of snapshots to keep. If using RAID, this will be multiplied by the number of EBS drives
KEEP_NUM_SNAPSHOTS = 8

# Path to the log file, if not set STDOUT will be used.
LOG_FILE = '/var/log/mysql-ebs-snapshot/mysql-ebs-snapshot.log'

############################
#  END - settings section  #
############################

instance_tag_name = ''


def setup_logging():
    if LOG_FILE:
        logger = logging.getLogger()
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        handler = logging.FileHandler(LOG_FILE)
        handler.setFormatter(formatter)
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    else:
        logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)


def snapshot_tag_str():
    return time.strftime("%Y%m%d_%H%M%S") + '_' + instance_tag_name


def get_snapshots():
    res = []
    unique = set()
    snapshots = ec2_conn.get_all_snapshots()
    for snapshot in snapshots:
        if 'Name' in snapshot.tags:
            name_tag = snapshot.tags['Name']
            if re.match(r"\d{8}_\d{6}_" + instance_tag_name + "$", name_tag):
                res.append(snapshot)
                unique.add(name_tag)

    return res, sorted(unique)


def clean_old_snapshots():
    if not KEEP_NUM_SNAPSHOTS:
        return
    logging.info("Performing cleanup of old snapshots...")
    (snapshots, tags) = get_snapshots()
    logging.info("Total snapshots found: %s" %len(tags))
    to_delete_count = len(tags) - KEEP_NUM_SNAPSHOTS
    if to_delete_count <= 0:
        return
    logging.info("Need to delete: %s" % to_delete_count)
    for i in range(to_delete_count):
        logging.info("Deleting old snapshot: %s" %tags[i])
        for snap in snapshots:
            if snap.tags['Name'] == tags[i]:
                snap.delete()
                logging.info("Deleted snapshot: %s" %snap.id)


def mysql_connect():
    global mysql_conn
    global db_cursor
    logging.info("Connecting to mysql...")
    mysql_conn = MySQLdb.connect(host=MYSQL_HOST, user=MYSQL_USERNAME, passwd=MYSQL_PASSWORD, db='')
    db_cursor = mysql_conn.cursor()
    logging.info("Connected.")


def mysql_get_binlog_position():
    # Oracle documentation recommends getting binlog position from a separate session
    # so we connect anew
    lconn = MySQLdb.connect(host=MYSQL_HOST, user=MYSQL_USERNAME, passwd=MYSQL_PASSWORD, db='')
    lcur = lconn.cursor()
    lcur.execute("SHOW MASTER STATUS")
    return lcur.fetchone()


def mysql_write_binlog_position_info(binlog_info):
    binlog_info_str = "MASTER_LOG_FILE='%s', MASTER_LOG_POS=%s" % (binlog_info[0], binlog_info[1])
    with open(MYSQL_DATA_DIR + '/binlog_info.txt', 'w') as f:
        f.write(binlog_info_str)


def flush_mysql_tables():
    logging.info("Flushing mysql tables...")
    db_cursor.execute("FLUSH TABLES WITH READ LOCK")
    logging.info("Flushing engine logs...")
    db_cursor.execute("FLUSH ENGINE LOGS")
    logging.info("Done flushing.")


def unlock_mysql_tables():
    db_cursor.execute("UNLOCK TABLES")
    logging.info("Mysql tables unlocked.")


def fs_freeze(mountpoint):
    if NO_FS_FREEZE:
        logging.info("Skipping FS freeze.")
        return
    logging.info("Freezing the filesystem...")
    subprocess.check_call([XFS_FREEZE_BIN, '-f', mountpoint])
    logging.info("Filesystem frozen.")


def fs_unfreeze(mountpoint):
    if NO_FS_FREEZE:
        logging.info("Skipping FS unfreeze.")
        return
    try:
        logging.info("Unfreezing filesystem at %s" %mountpoint)
        subprocess.check_call([XFS_FREEZE_BIN, '-u', mountpoint])
        logging.info("Filesystem unfrozen.")
    except:
        logging.info("Failed to unfreeze filesystem.")


def path_to_device_and_mountpoint(device):
    output = subprocess.check_output(['df', device])
    m = re.search("^(\S+)\s+\d+\s+\d+\s+\d+\s+\d+%\s+(.*)", output, re.MULTILINE)
    return m.group(1), m.group(2)


def list_disks(device):
    # check if raid device
    logging.info("Checking for RAID membership..")
    try:
        dev = os.path.basename(device)
        with open('/proc/mdstat') as f:
            contents = f.read()
            m = re.search(r"^%s : active \S+ (.*?)\n" %dev, contents, re.MULTILINE)
            devices_str = m.group(1)
            devices_arr = re.findall(r"(\w+?)\[\d+\]", devices_str)
            devices_arr = map(lambda s: "/dev/" + s, devices_arr)
            logging.info("Obtained raid devices: " + str(devices_str))
            return devices_arr
    except:
        pass
    logging.info("Not a RAID device: " + str(device))
    return [device, ]


def get_volume_ids(disks, instance_id):
    translated_disks = map(lambda s: re.sub(r"xvd(.*)", r"sd\1", s), disks)

    logging.info("Looking up volume-ids for: " + str(translated_disks))

    volumes = [v.id for v in ec2_conn.get_all_volumes() if v.attach_data.instance_id == instance_id and v.attach_data.device in translated_disks]
    logging.info("EC2 Volume IDs found: " + str(volumes))
    return volumes


def ebs_create_snapshots(volume_ids, extra_description_str):
    if NO_SNAPSHOT:
        logging.info("Skipping snapshot due to NO_SNAPSHOT.")
        return
    tag_str = snapshot_tag_str()
    if extra_description_str:
        extra_description_str = " (" + extra_description_str + ") "
    else:
        extra_description_str = ""

    for volume_id in volume_ids:
        logging.info("Creating snapshot for " + str(volume_id))
        snapshot = ec2_conn.create_snapshot(volume_id, "Created by mysql-ebs-snapshot" + extra_description_str)
        
        snapshot.add_tags({'Name': tag_str })
        logging.info("Snapshot started with id: %s, tag Name: %s" % (snapshot.id, tag_str))


def do_snapshot(mysql_data_dir):
    global ec2_conn
    global instance_tag_name
    global KEEP_NUM_SNAPSHOTS

    if os.environ.get('KEEP_NUM_SNAPSHOTS'):
        KEEP_NUM_SNAPSHOTS = int(os.environ.get('KEEP_NUM_SNAPSHOTS'))

    logging.info("########## STARTING SNAPSHOT ##########")
    logging.info("Mysql data dir: " + str(mysql_data_dir))
    (device, mount_point) = path_to_device_and_mountpoint(mysql_data_dir)
    logging.info("Device: %s." %device)
    logging.info("Mountpoint: %s." %mount_point)

    mysql_connect()
    try:
        global ec2_conn
        instance_metadata = boto.utils.get_instance_metadata()
        instance_id = instance_metadata['instance-id']
        ec2_region = instance_metadata['placement']['availability-zone'][0:-1]
        logging.info("Connecting to EC2...")
        ec2_conn = boto.ec2.connect_to_region(ec2_region, aws_access_key_id=AWS_KEY, aws_secret_access_key=AWS_SECRET_KEY)

        inst = ec2_conn.get_only_instances(instance_ids=[instance_id])[0]
        instance_tag_name = inst.tags['Name']
        tag_suffix = os.environ.get('TAG_SUFFIX')
        if tag_suffix:
            instance_tag_name = instance_tag_name + '_' + tag_suffix
        # for raid we'll need to extract actual disk list
        disks = list_disks(device)
        volume_ids = get_volume_ids(disks, instance_id)
        if len(volume_ids) < 1:
            raise Exception("No EBS volumes found for the specified device.")
        # sync the disk
        logging.info("Syncing to disk.")
        subprocess.check_call('sync')
        # if using mysql, flush table now
        flush_mysql_tables()

        binlog_pos = mysql_get_binlog_position()
        mysql_write_binlog_position_info(binlog_pos)
        extra_description_str = "binlog:%s@%s" % (binlog_pos[0], binlog_pos[1])

        # sync the disk again
        logging.info("Syncing to disk again.")
        subprocess.check_call('sync')

        # freeze the FS
        fs_freeze(mount_point)

        # create new snapshots and clean the old ones
        ebs_create_snapshots(volume_ids, extra_description_str)
        clean_old_snapshots()
    except:
        logging.exception("!!!!!!!!!! EXCEPTION !!!!!!!!!!")
        raise
    finally:
        fs_unfreeze(mount_point)
        unlock_mysql_tables()
    logging.info("########## SNAPSHOT FINISHED ##########")

if __name__ == '__main__':
    setup_logging()
    do_snapshot(MYSQL_DATA_DIR)
