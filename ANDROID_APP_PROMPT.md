# Prompt: build the Android app for pocket-share

Copy everything below into your AI coding tool. Also give it the repository URL:
<https://github.com/shadowlite07/pocket-share>

---

## Task

Build an Android app (`.apk`) that runs **pocket-share**, an existing Python
tool for sharing files and text between two devices on the same local network.

The app must be able to act as **either** role:

- **Server phone** — runs the tool, shares its own hotspot, serves files/text
- **Client phone** — opens the web page and downloads/sends

Read the source first. Everything is in one file: `teleport_share.py`
(~1900 lines, Python 3 standard library only).

## The single most important decision

**Do not reimplement the server in Kotlin.** The Python script is already
Android-compatible and is the product. Embed and run it unchanged.

The only Windows-specific code in the whole file is the clipboard, and it is
already guarded:

```python
def copy_to_clipboard(text: str) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "Clipboard is only wired up on Windows."
```

That check runs *before* any Windows API call, so on Android the clipboard is
simply a no-op and everything else works as-is. Verify this yourself before
assuming anything else needs porting.

## Recommended stack

**Kotlin + Chaquopy + WebView.**

Chaquopy embeds CPython into a Gradle Android app, so the existing script runs
unmodified and the app's UI is a `WebView` pointed at `http://127.0.0.1:8765`.
You are writing an app *shell*, not a second implementation. This also builds
on Windows, unlike buildozer/Kivy which require Linux or WSL.

If Chaquopy proves unworkable, `Kivy` + `python-for-android` is the fallback —
but note buildozer is Linux-only, so that path needs WSL2.

## Architecture

```
MainActivity
 ├── ForegroundService  (keeps the Python server alive when the screen locks)
 │    └── Chaquopy -> teleport_share.py -> ThreadingHTTPServer on 127.0.0.1:PORT
 └── WebView  ->  http://127.0.0.1:PORT   (the existing UI, unmodified)
```

The script already serves its own page, handles the PIN, does uploads,
downloads, texts and deletes. The app only has to start it, keep it alive, and
show it.

## Four requirements that are easy to get wrong

1. **Cleartext HTTP is blocked by default** since Android 9. Without
   `android:usesCleartextTraffic="true"` in the manifest (or a network security
   config permitting cleartext to the local subnet), the WebView will refuse to
   load `http://127.0.0.1:PORT`. This is fatal and silent if missed.

2. **A plain Activity is killed when the screen locks.** The server must run in
   a `ForegroundService` with a persistent notification, or transfers die the
   moment the user locks the phone.

3. **Apps cannot switch on the Wi-Fi hotspot programmatically** (restricted
   since Android 10). Provide a button that deep-links to
   `Settings.ACTION_WIRELESS_SETTINGS` and explains the step in the UI. Do not
   attempt `WifiManager.setWifiApEnabled`.

4. **Do not touch the clipboard code path** expecting it to work on Android —
   `copy_to_clipboard` intentionally does nothing there.

## Permissions

```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
<uses-permission android:name="android.permission.ACCESS_WIFI_STATE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_CONNECTED_DEVICE" />
<uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
```

Android 13+ also needs a runtime "Nearby devices" permission
(`NEARBY_WIFI_DEVICES`) if you show nearby Wi-Fi info. **No storage or media
permissions are needed** — files are served over HTTP and never read from the
phone's gallery. Keep it that way.

## HTTP API the app may need to call

The page uses these. Base URL is the server's address.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/login` | `{"pin":"123456"}` → `{"token":"..."}`; store it |
| `GET` | `/api/info` | server IP, port, data folder, whether a PIN is set |
| `GET` | `/api/files` | every shared file, newest first |
| `GET` | `/api/download?path=…` | stream a file back |
| `POST` | `/api/upload?name=…` | raw binary body, one file per request |
| `GET` | `/api/texts` | message history |
| `POST` | `/api/text` | `{"text":"…"}` |
| `DELETE` | `/api/file?path=…` | delete one file |
| `GET`/`POST` | `/api/settings` | read/update the clipboard auto-copy toggle |

Every endpoint except `/api/login` requires an `X-Auth-Token` header. Paths in
queries must be percent-encoded — filenames can contain any Unicode.

## Known behavioural wrinkle to solve

The script decides who sent something by comparing the client's IP to its own:

```python
def identify_sender(client_ip, local_ip):
    if ip in ("127.0.0.1", "::1"): return "laptop"
    if local_ip and ip == local_ip: return "laptop"
    return "phone"
```

That assumes a laptop-and-phone topology. When **a phone is the server**, both
connected devices look like "phones", so both are labelled `PHONE` and the
clipboard toggle appears where it should not. Pick a solution and justify it —
the cheapest is a `--identity` flag on the script defaulting to `auto`, with a
setting in the app to override it.

## UI expectations

- The WebView shows the existing page. Do not rebuild the UI.
- Use a dark/light aware theme consistent with the web page.
- Provide: start/stop server, show the LAN URL large with a copy button, open
  the hotspot settings, and an optional "Open browser" for the client role.
- Never allow the WebView to navigate off the local server origin.

## Acceptance criteria

- [ ] The app starts `teleport_share.py` and the page loads in the WebView
- [ ] Screen can be locked and transfers continue
- [ ] Another device on the hotspot can reach the page and download a file
- [ ] The PIN prompt appears and works
- [ ] Uploading a file from the client phone lands in `SharedFiles/`
- [ ] The server survives the activity being destroyed and recreated
- [ ] The app never crashes on rotate, on lock, or on Wi-Fi being toggled

## Constraints

- Do not add a cloud service, an account, or any network call outside the LAN.
- Keep the tool's "no app needed on the other device" property: a plain browser
  on any device must still be able to use it.
- Do not reformat or restructure `teleport_share.py`. If it needs a change,
  keep it minimal and backwards compatible — it is also used on Windows.