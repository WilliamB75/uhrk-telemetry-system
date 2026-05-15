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

The format is `mpegts` because it's the only format that still has outputed a video when the program is unexpectedly forceafully killed with `kill -9 $(pgrep mediamtx)`. The FPS may be increased if needed to 60FPS, however the temperature on the board rises much quicker reaching 60 ºC in ~5 minutes; although it doesn't start throttling until ~80 ºC so it is probably still perfectly fine. %path in this context refers to the location where the MediaMTX tool was installed, so if the steps have reproduced exactly, the destination of the recordsings will be at: `~/dev/MediaMTX/recordings/`. To increase accessibility, AFTER LANDING and MANUALLY, the file can be converted to .mp4 with `ffmpeg`.

4. The website should already have an iframe element inside it like this:

<iframe src="http://PI_IP:8889/cam" height="500"></iframe>

Note that this effectively tells the client to look into a video stream that is on the server, so the IP will never be localhost (127.0.0.1), but rather the actual IP address of the Pi. This works because MediaMTX exposes an endpoint that the client can read video directly from. Meaning, it could be read by any tool that listens for video streams.

That's it. If you encounter any issues with the camera hardware, refer to the final step in the [Camera Setup Guide](./camera-setup.md#)
