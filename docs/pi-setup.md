# Pi Setup

This is a short guide on how to set up the Pi Zero W. This tutorial assumes setup on a Linux machine.

## Requirements

- A computer running Linux (or patience to translate the commands into powershell)
- Raspberry Pi SBC (Preferrably Pi Zero 2W, or Raspberry Pi > 3)
- USB-A/C to USB-B (microusb) cable that can transmit data

## Flashing OS

1. Download the [Raspberry Pi Imager](https://www.raspberrypi.com/software/) from the official site, and install it on your system.

2. Run the imager with root-like permissions, this will avoid potential issues writing to the disk.

```sh
xhost +si:localuser:root
sudo rpi-imager
```

3. In the installer select the appropriate board and storage to write to. Ensure that you're not flashing your computer's disk! If unsure, run `lsblk` on your terminal.

4. For the OS, go into "Raspberry Pi OS (other)" and then select the Lite version. If encountering issues later on particularly regarding Wi-Fi, try to switch to the legacy version.

5. When asked to edit the configuration, select edit the configuration.

6. Pick an easy to remember and hard to miss-type username and password. In these settings, enable Wi-Fi and enter the name and password of your current Wi-Fi network. Then, in the advanced tab, enable SSH and keep it as loging in using password.

7. Once that is done, select save, and flash the disk. Then wait for it to finish writing into the disk.

8. Once the data has been written and verified, unplug and plug back in the SD card into your machine. Then, using the file explorer, or the terminal, mount the card.

The following two steps are optional, and only required if you want to set up a connection to the Pi board via ethernet over usb:

9. Navigate into the root directory of the partition called "bootfs", and edit the `cmdline.txt` file. Ensuring that it remains just one line, add the following text to the very end of the file: (there should be a space between the last word and the word "modules".

```txt
modules-load=dwc2,g_ether
```

10. After saving that file, edit `config.txt`, remove everything below `[cm4]`, and add to the bottom of the file:

```toml
[all]
dtoverlay=dwc2
```

11. Check that everything looks right by checking the file `firstrun.sh`. You can check it's contents with `cat`. Ensure that the configs seems to do everything that you told it, most importantly check: SSH and Wi-Fi capabilities if enabled.

12. To doublecheck that the hash of the wi-fi password is right, you may run:

```sh
wpa_passphrase SSID 'PASSWORD'
```

Replacing the values with your actual SSID (Wi-Fi name) and Wi-Fi password. Then compare that against the hash that will appear in the `cat` from `firstrun.sh`.

> [!note]
> The following steps in this section are optional, and only needed if the previous setup didn't work. It should be noted, that this will most likely only work with very old versions of PiOS. If it's your first time running the instructions, it is recommended to not go through them, running them from the terminal is recommended to reduce any errors involving file explorers. They assume that your current directory is the bootfs partition. You can get it's path with the lsblk command, and navigate to it with cd.

13. If experiencing issues with ssh, create a blank file called `ssh` in the root of the `bootfs` partition, the safest way to do it is with `touch`:

```sh
touch ssh
```

14. If experiencing issues with WiFi, create a file named `wpa_supplicant.conf` inside `bootfs` and write inside, replacing the strings in ALL CAPS in the network section, but keep the quotation marks. If relevant, change the country as well.

```txt
country=GB
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1

network={
    ssid="YOUR_WIFI_NAME"
    psk="YOUR_WIFI_PASSWORD"
}
```

15. Save everything. `cat` all of the modified files and ensure that they have the proper contents, then, unmount the SD card.

## Connecting into the Pi

The previous steps have prepared the SD card with the Pi OS ready to be accessible both via the wifi interface, or via a ethernet over USB. Both methods should work out of the box but the wired method is tried first for it's higher reliability.

1. Insert the SD card into the Pi

2. Connect the USB-B end of the cable into the data port of the Pi. In the Pi Zero, is the one closest to the middle of the board and the micro-HDMI connector. Then connect the other end to your machine.

3. If everything goes well, a green light in the oposite end of the SD card should start flashing shortly after. If so, the system is booting. After a minute or two, it should stop blinking and turn into a solid green light, with maybe ocasional blinks. To discard any issues later on, I like to give it up to 3 minutes, especially on the first boot.

> [!note]
> If the led is blinking in a rythmic repeating pattern, it usually means an error, count the amount of blinks between each pause, and look up the meaning online. For instance, 7 blinks means that the kernel could not be loaded, and it may mean that you have select the wrong OS for your board, ensure that you know which model the board is by checking it's name on it's back side.

4. Run `lsusb` from your machine. You should see an entry like: `Bus 003 Device 020: ID 0525:a4a2 Netchip Technology, Inc. Linux-USB Ethernet/RNDIS Gadget`. If you don't see it, ensure that your cable can transmit dta and that you have connected the cable to the right port. Alternatively, try disconnecting the power from the chip, then remove the sd card, connect it to your machine, and doublecheck the config files.

5. If the device appears, ensure that the device is reachable by pinging it. It's hostname that you set up on the installer (by default: `raspberrypi.local`):

```sh
ping HOSTNAME.local
```

6. Once the hostname is confirmed, try SSHing into it. The username is the one that you also set in the installer. After typing the command it will ask for the password of that user, if everything goes well, you will be in!

```sh
ssh USERNAME@HOSTNAME.local
```

7. If you had set up the Wi-Fi, try to see if it's in your home network. If available, go into your router's address in your web browser, and check in the attached devices if your hostname appears there. Alternatively, you can use nmap, although it may not always resolve the hostname of the Pi, change the IP depending on your local network, which you can get from (ip addr):

```sh
sudo nmap -sn 192.168.0.0/24 | grep "Nmap"
```

8. If you can ssh into it, but it does not appear on your router's attached devices, then from the Pi's terminal type, the credentials should already have been defined. If that does not work, check if the Wifi is turned with `rfkill list`, it should say that they are not blocked:

```sh
nmcli dev wifi connect YOUR_WIFI_NAME
```

