#!/bin/bash
set -B

####################### VARIABLES #######################
BACKUP_DIRECTORY=/media/raid1/Backups/imgs/$HOSTNAME/ # Make sure to include / on the end
#########################################################

# Check to see if script is being run as root
if [ $(id -u) -ne 0 ]; then
	printf "ERROR: Image Backup Script must be run as root. Try '$0'\n"
	exit 1
fi

# Check if required apps are installed
apps=( "rsync" )
for i in "${apps[@]}"
do
	if ! [ -x "$(command -v $i)" ]; then
		echo "ERROR: $i could not be found."
	exit 1
fi
done
#

# Check whether Backup directory exists
if [ ! -d $BACKUP_DIRECTORY ]; then
	echo "ERROR: Backup Directory [$BACKUP_DIRECTORY] does not exist."
	exit 1
fi
#

# Output Backup Started message
s_timestamp=$(date +'%s')
dt=$(date -d @$s_timestamp '+%m/%d/%Y %H:%M:%S');
printf "BACKUP PROCESS STARTED ON $dt\n"
#

# RSYNC ROOT to Backup Directory
echo "Syncing Backup Directory with ROOT"
rsync -aAXv --delete --exclude={"etc/fake-hwclock.data","/usr/lib/aarch64-linux-gnu/gstreamer1.0/gstreamer-1.0/gst-ptp-helper","/boot/*","/dev/*","/proc/*","/sys/*","/tmp/*","/run/*","/mnt/*","/media/*","/lost+found","/var/swap","/var/tmp/*","/var/log/*","/var/log.hdd/*"} / $BACKUP_DIRECTORY #>& /dev/null
if [ $? -ne 0 ]; then
	echo "ERROR: Failed to sync Backup Directory with ROOT."
	exit 1
fi

#Run 'sync' to ensure all I/O activity is complete
sync

# Output Image Backup Finished message
f_timestamp=$(date +'%s')
dt=$(date -d @$f_timestamp '+%m/%d/%Y %H:%M:%S');
printf "BACKUP PROCESS FINISHED ON $dt\n"
dur="$(($f_timestamp-$s_timestamp))"

h=$(( dur / 3600 ))
m=$(( ( dur / 60 ) % 60 ))
s=$(( dur % 60 ))

printf "Duration: %02d:%02d:%02d\n\n" $h $m $s

echo "Process completed successfully."
exit 0
