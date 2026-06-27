# YouTube Song Automation Bot

A command-line bot that creates a simple YouTube music video from one command:

1. Generates a beautiful 1280×720 thumbnail/title card from the song name.
2. Uses either a local audio file **or a YouTube link** as the song source.
3. Makes a video where that thumbnail stays on screen for the whole song.
4. Appends your provided 30–40 second outro video.
5. Optionally uploads the final video and thumbnail to your YouTube channel using the official YouTube Data API.

> **Important:** Use only songs/audio that you own, have licensed, or have explicit permission to reuse and upload. If using `--youtube-url`, make sure your use is permitted by the rights holder and complies with YouTube's terms.

---

## Requirements

- Python 3.10+
- FFmpeg installed and available in your terminal:
  - Windows: install from <https://ffmpeg.org/download.html> and add `bin` to PATH
  - macOS: `brew install ffmpeg`
  - Ubuntu/Debian: `sudo apt install ffmpeg`
- A Google Cloud OAuth client for YouTube upload access

Install Python packages:

```bash
pip install -r requirements.txt
```

---

## YouTube setup

1. Open Google Cloud Console.
2. Create or select a project.
3. Enable **YouTube Data API v3**.
4. Configure the OAuth consent screen.
5. Create OAuth credentials:
   - Application type: **Desktop app**
6. Download the JSON file.
7. Rename it to `client_secrets.json` and place it in this folder.

On the first upload, a browser window opens. Log in with the YouTube channel owner account and grant upload permission. A local `token.json` file is created so future uploads can run from one command.

Do **not** share `client_secrets.json` or `token.json`.

---

## Generate only, no upload

### From a local audio file

```bash
python bot.py create-upload \
  --song-name "Your Song Name" \
  --artist "Artist Name" \
  --audio-file "/path/to/song.mp3" \
  --outro-file "/path/to/outro.mp4" \
  --no-upload
```

### From a YouTube link you are allowed to reuse

```bash
python bot.py create-upload \
  --song-name "Your Song Name" \
  --youtube-url "https://www.youtube.com/watch?v=VIDEO_ID" \
  --outro-file "/path/to/outro.mp4" \
  --no-upload
```

If you omit `--song-name`, the bot will try to use the YouTube video's title automatically:

```bash
python bot.py create-upload \
  --youtube-url "https://www.youtube.com/watch?v=VIDEO_ID" \
  --outro-file "/path/to/outro.mp4" \
  --no-upload
```

Generated files will appear in `outputs/`.

---

## Generate and upload to YouTube

### Upload from local audio

```bash
python bot.py create-upload \
  --song-name "Your Song Name" \
  --artist "Artist Name" \
  --audio-file "/path/to/song.mp3" \
  --outro-file "/path/to/outro.mp4" \
  --title "Your Song Name - Official Audio" \
  --description "Thanks for listening. Subscribe for more music." \
  --tags "music,official audio,artist name" \
  --privacy private
```

### Upload from YouTube link

```bash
python bot.py create-upload \
  --song-name "Your Song Name" \
  --youtube-url "https://www.youtube.com/watch?v=VIDEO_ID" \
  --outro-file "/path/to/outro.mp4" \
  --title "Your Song Name - Official Audio" \
  --description "Licensed upload. Thanks for listening." \
  --tags "music,official audio" \
  --privacy private
```

Privacy options:

- `private` default and safest for testing
- `unlisted`
- `public`

---

## Single-command pattern

After first OAuth login, every new video can be produced and uploaded with one command:

```bash
python bot.py create-upload --song-name "Song" --audio-file song.mp3 --outro-file outro.mp4 --privacy public
```

Or with a YouTube link:

```bash
python bot.py create-upload --song-name "Song" --youtube-url "https://www.youtube.com/watch?v=VIDEO_ID" --outro-file outro.mp4 --privacy public
```

---

## Notes

- The thumbnail is saved as `thumbnail.jpg` and also uploaded as the YouTube thumbnail.
- The final video is encoded at 1920×1080, 30 fps, H.264/AAC.
- YouTube-link audio extraction uses `yt-dlp` and FFmpeg. Keep `yt-dlp` updated if YouTube changes something: `pip install -U yt-dlp`.
- The outro is automatically resized/padded to 1920×1080.
- Category ID defaults to `10`, which is YouTube's Music category.
- If your channel needs extra metadata, add it through `--description`, `--tags`, and `--title`.
