# Water meter backup

Program backup updated 22 November 2026 from the supplied Raspberry Pi archive and the verified live dashboard at https://brookes.ch/water/.

## Files

- water_meter_logger.py collects hourly readings from both Tuya meters.
- check_tuya_meter.py provides the Tuya API client, including retries for response code 501.
- daily_alarm_email.py checks 72 hours of alarm flags, including Cover alarm (4096), and labels relocated-meter alarms by the changeover.
- upload_website.py publishes CSV snapshots to brookes.ch.
- upload_two_meters.sh copies both CSV files to Google Drive.
- dashboard.html is the live water dashboard source, deployed as sites/brookes.ch/water/index.html on the existing host.

The repository-root index.html and privacy.html are existing separate pages. The water dashboard source is dashboard.html.

## Changeover

The upstream meter moved downstream of the Garden split on 18 November 2026 at 12:50 Cyprus time (UTC+02:00). Before this boundary it measured Total property. From the boundary onward it measures House. The dashboard preserves both eras.

Recorded counters were 74.883 m³ for the relocated meter, 61.815 m³ on the Garden physical display, and 4410.998 m³ on the official meter. Neither counter reset and no water was used during the move. The dashboard uses 61.814 m³ as the Garden boundary to match Tuya readings immediately before and after the move.

## Existing Pi configuration

These are program files for the existing /home/pi/water-meter installation. Credential configuration, SSH keys, rclone configuration, CSV histories, and installed systemd service/timer definitions are stored separately on the Pi. This repository snapshot does not contain a verified export of those service/timer definitions.

The existing schedules are hourly meter collection at :00, Drive upload at :05, website upload at :06, and daily alarm checking at 09:00 Cyprus time. Updating this repository does not deploy files to the Pi or website.
