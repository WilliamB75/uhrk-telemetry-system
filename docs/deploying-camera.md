# Deploying the camera for the live-ground station

## Requirements

- SSH access to the raspberry Pi (see the [Pi Setup guide](./pi-setup.md))
- A properly set up camera (see the [Camera Setup guide](./camera-setup.md))
- Access to the ground station website

## Procedure

1. Install MediaMTX, which is the program that will let the Pi stream it's contents into the website

```sh
mkdir dev
cd dev
mkdir MediaMTX
cd MediaMTX
wget https://github.com/bluenviron/mediamtx/releases/download/v1.18.1/mediamtx_v1.18.1_linux_arm64.tar.gz
tar -xzvf mediamtx_v1.18.1_linux_arm64.tar.gz
```

I wouldn't recommend going directly into the GitHub page because this version just works, and it would be better not risking any breakage in any future version.

2. Configure MediaMTX to be able to read the camera feed from the Raspberry Pi. From the directory where you were in, edit `mediamtx.yml`. Go to the very end of the file where there should be a `paths:` section already defined, add the necessary lines so that it looks like this:

```yaml
paths:
  cam:
    source: rpiCamera
    rpiCameraWidth: 640
    rpiCameraHeight: 480
    rpiCameraFPS: 30
    record: yes
    recordPath: "%path/recordings/%Y-%m-%d_%H-%M-%S"
    recordFormat: mpegts
```

The format is `mpegts` because it's the only format that still has outputed a video when the program is unexpectedly forceafully killed with `kill -9 $(pgrep mediamtx)`. The FPS may be increased if needed to 60FPS, however the temperature on the board rises much quicker reaching 60 C in ~5 minutes. Note that the board doesn't start throttling until ~80 C so it is probably still perfectly fine.

4. The website should already have an iframe element inside it like this:

_STILL NEED TO FIGURE OUT_
