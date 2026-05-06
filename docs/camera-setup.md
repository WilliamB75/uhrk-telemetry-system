# Camera Setup Raspberry Pi Guide

## Requirements

- Raspberry Pi (this was tested for the Pi Zero 2W)
- SSH access to the Raspberry Pi (see [the other guide](./pi-setup.md))
- A Pi Camera (tested with an HQ camera and a lens)
- FPC ribbon cable that fits the Pi and the camera
- A wall charger (recommended due to high power usage, laptop may not be enough)

## Steps

0. Connect the the camera to the rasberry pi by connecting each of them to the FPC cable. The silver pads should be facing the PCB. To insert the FPC connector gently pull out the gray/dark brown tab on the connector on the board, insert the ribbon cable, and then press the tab back into it's original position.

1. Power on the board, using the power connector (the one near the MIPI (camera) port is recommended), preferably to an outlet.

2. SSH into the board

3. Install the necessary packages

```sh
sudo apt install libcamera-apps-lite ffmpeg
```

If you encounter any issues with the libcamera (`rpicam`) then try installing the full libcamera suite `libcamera-apps`.

4. Configure the Raspberry Pi to have enabled the I2C prototocol to talk to the camera. It may be on by default.

Enter the Pi configurator with:

```sh
sudo raspi-config
```

Navigate down to the `I2C` section and enable it. Then go back, move the arrow to the side twice, and press enter on `Finish`.

Try proceding to the next part, if it fails to show the hardware devices, try rebooting the Pi after this step.

5. Ensure that the Pi can talk to the hardware

```sh
v4l2-ctl --list-devices
```

You should see something like the following at the very beginning of the output:

```
unicam (platform:3f801000.csi):
	/dev/video0
	/dev/video1
	/dev/media0
```

It may have a different interface name, but ensure that the device `/dev/video0` exists.

6. Try taking a still image with:

```sh
cd images
rpicam-still -o $(date +%s).jpg
```

7. Once that is completed, you will need to retrieve it on your computer to see it. You may use `rsync` for it, however, for ease of use I recommend setting up an SFTP network connection in your file explorer. Most file explorers should support that functionality, and it should be available under the "Network" tab. When adding a new device, set the server address to the same SSH address that you used to connect to it from your machine, and append the directory that you're interested in in the end. Note that by default you'll have access to all of the directories regardless. The server address might look something like this:

```sh
sftp://non@pi.local/home/
```

8. From your own file browser, try opening the image. Depending on the camera and lens that you used you may see different things:

    - Solid color: Probably you're using an HQ camera without the lens attached, or the lens may not be at the correct position to capture the image. I recommend taking multiple pictures at different positions of the lens until it has focused enough for what you want. A good rule of thumb is, the higher the file size, the better resolution the image is, since it means that there is more detail and JPG cannot compress it as well. Note that the camera is very sensitive to focus changes, so when you're near the optimum point, decrease your rotation steps. Recording a video may be faster if you know exactly how far did you scroll when it reached the optimum focus. You can record a video with:
    
    ```
    sleep 10; rpicam-vid -t 60000 --codec libav -o test2.mp4
    ```

    - Magenta/Pink/Purple tint: You are probably using a NO IR-CUT lens which means that you're seeing the IR from the image. You probably don't want to use that lens for the rocket.
    
    - Blurry Picture: Adjust the lens position if there is, or consider using another camera


That should make you end up with a working camera for your Pi. To see how to deploy it on the rocket, particularly for the live website, see the [deploying camera](deploying-camera.md).

If you encounter any issues, DM me on Discord.
