# IPTV Player

Windows desktop player for an Xtream Codes server.

## Legal Disclaimer

**Important Notice:** This software is a generic, open-source media player designed exclusively to parse and stream user-provided Xtream API credentials and playlists. 

* **No Content Supplied:** The developer does not provide, host, bundle, or maintain any media, streams, playlists, or content of any kind. 
* **User Responsibility:** Users must provide their own legally acquired content or subscription credentials. It is the sole responsibility of the end-user to ensure that their source of content complies with local laws and regulations.
* **No Endorsement of Infringement:** The developer does not endorse, facilitate, or condone the streaming of copyrighted material without explicit permission from the respective rights holders.
* **Limitation of Liability:** This application is provided "as is" without warranty of any kind. The developer accepts no liability for any misuse of this software, data loss, or legal actions resulting from copyright infringements committed by the end-user.


## Download

On 64-bit Windows, download the setup program from the latest release:

[IPTV-Player-Setup.exe](https://github.com/LewisCarter02/iptv-player/releases/latest/download/IPTV-Player-Setup.exe)

Double-click that file. If Windows says it protected your PC, choose **More info**, then **Run anyway**. The setup copies IPTV Player onto this PC, puts an **IPTV Player** shortcut on the desktop, and opens the player. The shortcut uses the application logo.

Playback needs the free 64-bit [VLC media player](https://www.videolan.org/vlc/). If VLC is not already installed, the setup offers to install it.

## Install from source

You need 64-bit Windows and 64-bit Python 3.10 or newer. [Python for Windows](https://www.python.org/downloads/) should be installed with **Add python.exe to PATH** ticked. Git is required for the clone command.

From PowerShell:

```powershell
git clone https://github.com/LewisCarter02/iptv-player.git
Set-Location iptv-player
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

`install.ps1` creates a virtual environment, installs PyQt6, python-vlc, and requests, installs 64-bit VLC if it is missing and winget can do that, and puts an **IPTV Player** shortcut on the desktop.

The player loads `libvlc.dll`, so Python and VLC must both be 64-bit. If VLC is installed somewhere other than `C:\Program Files\VideoLAN\VLC`, set the `VLC_HOME` environment variable to that folder. Playback starts libVLC with `--avcodec-hw=any` so HD streams can use hardware decoding.

## Run

Double-click **IPTV Player** on the desktop. That opens one window and does not open a command prompt. The first time, sign in with the server URL (for example `http://example.com:8080`), username, and password, then choose a channel. After that, opening the icon signs in and starts the last channel you were watching.

From this folder, the same window can also be started with:

```bat
.venv\Scripts\pythonw.exe main.py
```

`pythonw.exe` is what keeps the app to a single window. `python.exe` also works, and it leaves a console open behind the player.

A successful sign-in is encrypted with Windows DPAPI and stored at:

`%APPDATA%\IPTVPlayer\config.json`

The password in that file can only be decrypted by the same Windows user on this PC. The next launch signs in automatically and plays the last channel, movie, or episode. Use **File → Log out** to erase the saved account.

## Controls

- Use **Live TV**, **Movies**, or **Series** at the top of the window.
- Click a category to load that list. **Favourites** at the top of the categories shows what you have saved for the current section.
- The search box searches every category in Live TV, Movies, or Series. Inside a series, it searches that series' episodes.
- Select a channel, movie, or series and press **Favourite**, or use the right-click menu. Favourites are kept for this server login.
- Double-click a channel or movie to play it. Double-click a series to open its episodes, then double-click an episode.
- Movies and episodes show a timeline under the video. Drag it to move through the title.
- Play, Pause, Stop, Mute, and the volume slider sit under the video.
- Press **F** or double-click the video to enter fullscreen. Press **Esc**, **F**, or **Exit fullscreen** to leave it. **F** still types normally while a text field is focused.

## If something fails

- **Sign-in failed:** the panel rejected the username or password, or the account is not active.
- **Server unreachable:** the URL or port is wrong, or the server is offline. Include `http://` or `https://` and the port.
- **Playback failed:** VLC could not open that `.ts` stream. Try another channel. A missing or 32-bit VLC install also produces this error.
- **DLL load error:** uninstall 32-bit VLC if both copies are present, install 64-bit VLC, and run the app with 64-bit Python.
- Windows Firewall may ask for permission the first time Python or VLC opens a stream. Allow access on the network you use.

Channel logos load in the background for the rows on screen. A channel without a logo, or a logo the server does not serve, still plays.
