# pocket-share

Share files and text between a Windows laptop and a phone on the same Wi-Fi
(typically a phone hotspot). No app to install on either device — both just
open the same web page.

- **Python standard library only.** No `pip install`, no virtualenv, no build step.
- **Any size of file.** Uploads stream to disk in 256 KB blocks, so a 4 GB
  video costs the same memory as a 4 KB text file.
- **Local network only.** Nothing leaves your hotspot; nothing is uploaded to
  any cloud service.

## Why

You are on a hotspot, your phone's battery is low, and you need to move a photo
to the laptop — or a file you just finished on the laptop over to the phone.
Normally that means a cable, an app, or a cloud upload. This is one command and
one URL.

## Requirements

Python 3.9+ on Windows. Nothing else.

## Run it

```
python teleport_share.py
```

It prints the address to open on each device:

```
  On the LAPTOP:
      http://localhost:8765

  On the PHONE (same Wi-Fi / hotspot):
      http://192.168.181.50:8765
```

Then open the same page on both. Useful flags:

```
python teleport_share.py --port 9000     # different port
python teleport_share.py --host 192.168.181.50   # only listen on that adapter
```

## Folder layout

Everything lives in `SharedFiles/`, next to the script:

```
SharedFiles/
  FromPhone/     files received from the phone
  FromLaptop/    files sent from the laptop
  Texts/         timestamped .txt of each message sent
  _inbox_tmp/    partial uploads (cleared on startup, never listed)
  .teleport_index.json
```

Files you copy into `SharedFiles\` by hand are picked up automatically — you do
not have to use the page to share something.

## Filenames are treated as hostile input

Anything a browser sends is untrusted, so incoming names are sanitised before
they touch the disk:

| Sent | Stored as |
|---|---|
| `../../secret.txt` | `secret.txt` |
| `C:\Windows\System32\evil.dll` | `evil.dll` |
| `CON.txt` | `_CON.txt` (a reserved Windows device name) |
| `a<b>c:d.txt` | `a_b_c_d.txt` |
| `photo.jpg` (already exists) | `photo (2).jpg` |

Nothing is ever overwritten silently, and nothing can be written outside
`SharedFiles\`.

## If the phone cannot connect

In order of likelihood:

1. **Windows Firewall** — allow the app on **Private networks** when prompted.
   If the Wi-Fi profile is set to Public, inbound connections are blocked
   entirely. Fix from an Administrator prompt:
   ```
   netsh advfirewall firewall add rule name="pocket-share" dir=in action=allow protocol=TCP localport=8765 profile=any
   ```
2. **Wrong network** — the laptop must be on the hotspot, not on office Wi-Fi.
   Windows only routes through one adapter at a time.
3. **AP isolation** — some phone hotspots block device-to-device traffic
   entirely. There is no setting on the laptop that can fix this; you have to
   change it in the hotspot settings or use a different hotspot.

## Status

Built and working:

- [x] Runs on both devices, prints the phone URL, detects the local IP
- [x] Send files **from the phone** to the laptop, with progress bars
- [x] Filename sanitising, collision handling, streaming for large files
- [x] Live "Received" list on the laptop page

Next:

- [ ] Download files **on the phone** from what the laptop has shared
- [ ] Send text in both directions (with optional auto-copy to the Windows
      clipboard, and a Copy button on the phone)
- [ ] Merged live history of files and text on both pages
- [ ] Optional PIN, QR code on the console, launcher script, full README

## Security notes

This is a tool for your own hotspot, and it is honest about what that means:

- Traffic is plain HTTP. The optional PIN stops someone who opens the URL, but
  it does not stop someone running a packet sniffer on the same network.
- There is no HTTPS, because Python's standard library can use a certificate
  but cannot generate one.
- Lock your hotspot with a password if the network is not just you.