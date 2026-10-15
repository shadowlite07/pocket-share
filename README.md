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

**As an app (Windows):**

```
python pocket_share.py
```

A small window starts the server for you, shows both addresses with Copy and
Open buttons, reveals the PIN on demand, opens the data folder, and shows live
activity so you can see transfers happening without watching a console.

**As a command line tool:**

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

Useful flags:

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
- [x] Live file list on both pages, auto-updating
- [x] Download any listed file on either device, including files copied into
      `SharedFiles\` by hand
- [x] Files the laptop shares are started automatically on the phone — the
      phone fetches them without you finding them in a list (Android Chrome
      still asks you to confirm with **Keep**, see below)
- [x] Send text from the phone — an icon button opens a box, and the message
      lands on the laptop page and on your Windows clipboard
- [x] Send text from the laptop, with a **Copy** button on the phone
- [x] 6-digit PIN lock, with sessions and brute-force throttling

Next:

- [ ] QR code on the console, double-click launcher script

## Security

The page is locked behind a **6-digit PIN**, which lives in a `.env` file next
to the script:

```
.env
  PIN=641867
```

That file is in `.gitignore` and must never be committed or shared. It is
created automatically on first run with a **random** PIN, which is printed in
the console at startup — so there is no guessable default to forget about.
Edit it any time to change the PIN.

Set `PIN=` (empty) to switch the lock off.

Both devices ask for the PIN the first time. The correct PIN is exchanged for a
random token that the browser keeps, so you only type it once per device (and
again after the laptop restarts — sessions are deliberately not saved to
disk). Guessing is slowed down: 10 wrong tries from one address locks it out
for a minute.

Be honest about what this does and does not do:

- It stops anyone who opens the URL from browsing, downloading or **deleting**
  your files.
- Traffic is still plain HTTP, so it does not stop someone running a packet
  sniffer on the same network, who could capture the PIN or a token.
- There is no HTTPS, because Python's standard library can use a certificate
  but cannot generate one.
- Lock your hotspot with a password if the network is not just you.

Deleting also bypasses the Recycle Bin, so *Delete all files* is gone for
good.

## Deleting

Each file has a **Delete** button. You have to tap it twice — the first tap
turns it red and asks *Really delete?*, and it disarms itself after 4 seconds
if you change your mind. **Delete all files** at the bottom works the same way
and then asks once more. **Clear all messages** in the Messages card empties
`saved messages` the same way, and leaves shared files alone.

Deletion goes through the same checks as a download, so a crafted request
cannot reach outside `SharedFiles\`. Internal bookkeeping files are never
deletable this way.

Two things worth knowing:

- **There is no undo, and deleting bypasses the Recycle Bin.** `Delete all
  files` cannot be recovered from.
- Saved messages under `Texts\` are kept by *Delete all files* — only the
  transferred files are removed.

## Text

Tap the ✎ button in the header to type a message, on either device. Whatever
you send shows up on the other screen within a couple of seconds, and every
message has a **Copy** button so you can lift it onto that device's clipboard.

On the laptop there is one extra behaviour: text arriving **from the phone** is
copied to the Windows clipboard automatically, so you can paste it straight
into whatever you were working in with Ctrl+V. Messages that made it into the
clipboard are tagged *past ready* in the history.

The clipboard switch only appears on the laptop page, because there is no
Windows clipboard on the phone. Turn it off and the message is still saved and
displayed — it just no longer takes over your clipboard.

Text typed on the laptop never touches the clipboard: it is your own text, and
you may well still want to paste it elsewhere.

Every message is also written to `SharedFiles/Texts/` as
`YYYYmmdd-HHMMSS_from-phone.txt`, so nothing is lost if you want to grep
through old notes later.

### Why the Copy button uses an old API

`navigator.clipboard` — the modern clipboard — only works in a **secure
context**. `http://localhost` counts as one, but `http://192.168.181.50` does
not, so on the phone that API simply does not exist. The button therefore tries
it first and falls back to selecting a throwaway `<textarea>` and calling
`document.execCommand('copy')`, which still works over plain HTTP. If even that
is refused, the button says **Press & hold** so the text is at least easy to
select by hand.

The Windows clipboard is written through `ctypes` and Win32 — no `pip`
install, and no hidden tkinter window popping up.

## How a file actually reaches the phone

The laptop runs the server, so every file passes through it — the laptop is
the origin, not a relay it can bypass. The phone is what saves the file, by
fetching it over HTTP; nothing else can write to the phone's storage without
an app installed on it.

So when you share a file from the laptop, the phone's open page notices it
within a couple of seconds and starts saving it by itself. Which files it takes:

- Anything the laptop shared or you copied in by hand
- Anything the phone itself sent — skipped, it already has those

Already-saved files are remembered in the browser, so reloading the page
never re-downloads the history. If the phone's page was closed when something
was shared, it is simply waiting in the list when you reopen it.

### What you'll still see on the phone

The page starts the download for you, so the file turns up on its own instead
of you hunting for it in the list. Android Chrome then shows a **Discard /
Keep** prompt, because the page is served over plain `http://` and Chrome
does not consider an insecure download trustworthy. Tap **Keep** and it saves.
iOS Safari does something similar on some versions.

This is a browser security decision, not something the server can turn off.
If the prompt bothers you, `chrome://flags/#allow-insecure-downloads` in
Chrome removes it. The alternative — HTTPS — needs a certificate, and a
self-signed one is not trusted by Chrome either, so it may not help while
adding a "connection is not private" warning of its own.

## Safety on the download side

The phone asks for files by name, so the same hostile-input rules apply in
reverse. A request is refused unless it survives three independent checks: no
`..` segments, no absolute path, and — after resolving symlinks — the result
must still be inside `SharedFiles\`. Internal files (`.teleport_index.json`,
`_inbox_tmp\`, `Texts\`) are never listed and never served.

## Security notes

This is a tool for your own hotspot, and it is honest about what that means:

- Traffic is plain HTTP. The optional PIN stops someone who opens the URL, but
  it does not stop someone running a packet sniffer on the same network.
- There is no HTTPS, because Python's standard library can use a certificate
  but cannot generate one.
- Lock your hotspot with a password if the network is not just you.

## Android

The Python server runs unchanged on Android — the clipboard is the only
Windows-specific part and it is already guarded. If you want a native app,
**[ANDROID_APP_PROMPT.md](ANDROID_APP_PROMPT.md)** is a ready-made brief for an
AI coding tool, covering the stack (Chaquopy + WebView, so the script is
embedded rather than rewritten) and the four Android traps that break this
particular app.

A quick way to try it before writing any app: run the script in **Termux**
(F-Droid, not the Play Store), `pkg install python`, then
`python teleport_share.py`.

## License

MIT — see [LICENSE](LICENSE).