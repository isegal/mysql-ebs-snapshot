# mysql-ebs-snapshot
An enhanced version of the consistent EBS snapshot tool for mysql on AWS, featuring auto-detection and support for RAIDed volumes and saving of binary log position information for replication.

This tool performs a live EBS snapshot of the volume containing mysql data directory. To make the data consistent, it relies on MySQL's FLUSH meshanism, the operating system's sync command, as well as XFS filesystem freeze capability. It can be used to create backups, as well as new MySQL replicas. When a snapshot is created, the binary log position is recorded into the file named binlog_info.txt within the mysql data directory. Upon launching a new replica or restoring from the backup, MySQL will issue a warning about an unclean shutdown and perform a recovery step. This is perfectly normal and neccessary, as we do not rely on shutting down mysql when we create the point-in-time backup snapshot. Instead we rely on the transactional nature of MySQL. This in term allows live backups and replication spawning with 0 downtime.

Features:
* Automated detection of volume IDs based on the specified path of the mysql data directory.
* Support for Linux Software RAID arrays (if multiple drives are detected, the snapshot will be performed on all of them).
* Detecting and recording of the master binary log position, which can be used for creating replicated MySQL servers.

Dependencies:
* Python language 2.7.
* Python Boto API version 2.2.2 or higher.
* Python MySQLdb version 1.2.3 or higher.
* XFS filesystem on the drive that contains MySQL data directory.
* xfsprogs 3.1.7 (specifically xfs_freeze tool).

(Tested on Ubuntu 14.04.4 LTS running on AWS with Percona MySQL server 5.6.16)

## Installation Example (Ubuntu)

1) Install dependencies:
```
pip install boto
pip install mysql-python
apt-get install xfsprogs
```

2) Create the directory for log files:
```
mkdir /var/log/mysql-ebs-snapshot/
```

3) Create a mysql user for the backup script:
```
mysql -e "GRANT RELOAD, REPLICATION CLIENT ON *.* TO backup@'localhost' IDENTIFIED BY '<password_for_backup_user>'"
```

4) Create an AWS IAM user for the backup script with the following policy.
```
{
"Version": "2012-10-17",
"Statement": [
{
"Effect": "Allow",
"Action": [
        "ec2:CreateSnapshot",
        "ec2:CreateTags",
        "ec2:DescribeAvailabilityZones",
        "ec2:DescribeSnapshots",
        "ec2:DescribeTags",
        "ec2:DescribeVolumeAttribute",
        "ec2:DescribeVolumeStatus",
        "ec2:DescribeVolumes"
      ],
"Resource": "*"
}
]
}
```
(Note: For better security, you may want to use a more restrictive policy by limiting the resources to a specific instance and snapshots ARNs.)

download the AWS key and AWS secret key for the user.

5) Place the mysql-ebs-snapshot.py in /usr/local/bin

6) Edit mysql-ebs-snapshot.py configuration section. At the very minimum the following values will need to be configured:
```
AWS_KEY = '<< PASTE YOUR AWS KEY HERE >>'
AWS_SECRET_KEY = '<< PASTE YOUR AWS SECRET KEY HERE >>'
MYSQL_PASSWORD='<< PASTE THE PASSWORD FOR BACKUP USER >>'

```

7) Ensure that the path to your mysql data directory is correct:
```
MYSQL_DATA_DIR = '/var/lib/mysql'
```

8) Ensure that the path to xfs_freeze tool is correct:
```
XFS_FREEZE_BIN = '/usr/sbin/xfs_freeze'
```

The tool can be run from the command line or from crontab.

NOTE: The command produces no output. All messages are logged into a log file.

The results of the run are written into a log file which is by default located at /var/log/mysql-ebs-snapshot/mysql-ebs-snapshot.log

Below is a log example of a successful run:
```
2015-03-30 13:37:36,337 - INFO - ########## STARTING SNAPSHOT ##########
2015-03-30 13:37:36,337 - INFO - Mysql data dir: /var/lib/mysql
2015-03-30 13:37:36,364 - INFO - Device: /dev/md0.
2015-03-30 13:37:36,364 - INFO - Mountpoint: /mnt.
2015-03-30 13:37:36,364 - INFO - Connecting to mysql...
2015-03-30 13:37:36,368 - INFO - Connected.
2015-03-30 13:37:36,386 - INFO - Connecting to EC2...
2015-03-30 13:37:36,729 - INFO - Checking for RAID membership..
2015-03-30 13:37:36,730 - INFO - Obtained raid devices: xvdb[2] xvdf[1](W)
2015-03-30 13:37:36,731 - INFO - Looking up volume-ids for: ['/dev/sdb', '/dev/sdf']
2015-03-30 13:37:37,168 - INFO - EC2 Volume IDs found: [u'vol-91ed3892']
2015-03-30 13:37:37,168 - INFO - Syncing to disk.
2015-03-30 13:37:37,570 - INFO - Flushing mysql tables...
2015-03-30 13:37:50,355 - INFO - Flushing engine logs...
2015-03-30 13:37:50,358 - INFO - Done flushing.
2015-03-30 13:37:50,359 - INFO - Syncing to disk again.
2015-03-30 13:37:50,391 - INFO - Freezing the filesystem...
2015-03-30 13:37:50,946 - INFO - Filesystem frozen.
2015-03-30 13:37:50,946 - INFO - Creating snapshot for vol-91ed3892
2015-03-30 13:37:51,612 - INFO - Snapshot started with id: snap-ba17997f, tag Name: 20150330_133750_db5
2015-03-30 13:37:51,612 - INFO - Unfreezing filesystem at /mnt
2015-03-30 13:37:51,617 - INFO - Filesystem unfrozen.
2015-03-30 13:37:51,618 - INFO - Mysql tables unlocked.
2015-03-30 13:37:51,618 - INFO - ########## SNAPSHOT FINISHED ##########
```
