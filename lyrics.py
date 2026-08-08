import os
import re
import sys
import time
import requests

from mutagen.mp3 import MP3
from mutagen.id3 import ID3, USLT, SYLT, ID3NoHeaderError


LRCLIB_URL = "https://lrclib.net/api/get"

USER_AGENT = "MP3LyricsEmbedder/1.0"


def clean_filename(name):
    """Make a filename safe for Windows/Linux/macOS."""
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def title_from_filename(filepath):
    """
    Get the song title from the MP3 filename.

    Examples:
        Crashing Hard.mp3       -> Crashing Hard
        Crashing Hard...mp3     -> Crashing Hard
        Crashing Hard...2.mp3   -> Crashing Hard
        Crashing Hard (2).mp3   -> Crashing Hard
        Crashing Hard [2].mp3   -> Crashing Hard
        Crashing Hard - 2.mp3   -> Crashing Hard

    The duplicate suffix is removed only when it looks like a copy/
    duplicate number rather than being part of the actual title.
    """

    filename = os.path.basename(filepath)

    # Remove extension
    title = os.path.splitext(filename)[0]

    # Remove trailing "..." used by some file-renaming/download tools
    title = re.sub(r'\s*\.{2,}\s*$', '', title)

    # Remove common duplicate-number suffixes:
    #
    # "Song (2)"
    # "Song [2]"
    # "Song {2}"
    # "Song - 2"
    # "Song _ 2"
    #
    # Only numbers >= 2 are considered duplicates.
    title = re.sub(
        r'\s*(?:\(\s*[2-9]\d*\s*\)|\[\s*[2-9]\d*\s*\]|\{\s*[2-9]\d*\s*\}|[-_]\s*[2-9]\d*)\s*$',
        '',
        title
    )

    # Handle names such as:
    # "Song...2"
    # "Song... 2"
    title = re.sub(r'\s*\.{2,}\s*[2-9]\d*\s*$', '', title)

    # Remove trailing whitespace
    title = title.strip()

    return title


def get_mp3_info(filepath):
    """Read artist, title, album and duration from an MP3."""

    try:
        audio = MP3(filepath)

        tags = audio.tags

        def get_tag(name):
            if not tags:
                return None

            value = tags.get(name)

            if value:
                return str(value[0]).strip()

            return None

        # Read metadata
        title = get_tag("TIT2")
        artist = get_tag("TPE1")
        album = get_tag("TALB")

        # -------------------------------------------------
        # If title metadata is missing, use filename
        # -------------------------------------------------

        if not title:
            title = title_from_filename(filepath)

        duration = round(audio.info.length)

        return {
            "title": title,
            "artist": artist,
            "album": album,
            "duration": duration,
        }

    except Exception as e:
        print(f"  Error reading MP3: {e}")
        return None


def get_lyrics(info):
    """Ask LRCLIB for the lyrics."""

    params = {
        "track_name": info["title"],
        "artist_name": info["artist"],
        "album_name": info["album"] or "",
        "duration": info["duration"],
    }

    headers = {
        "User-Agent": USER_AGENT
    }

    try:
        response = requests.get(
            LRCLIB_URL,
            params=params,
            headers=headers,
            timeout=15
        )

        if response.status_code == 404:
            return None

        response.raise_for_status()

        return response.json()

    except requests.RequestException as e:
        print(f"  API error: {e}")
        return None


def parse_lrc(lrc_text):
    """
    Convert LRC timestamps into the format required by ID3 SYLT.

    Returns:
        [(milliseconds, lyrics_line), ...]
    """

    result = []

    pattern = re.compile(
        r"\[(\d+):(\d{2})(?:\.(\d{1,3}))?\](.*)"
    )

    for line in lrc_text.splitlines():

        match = pattern.match(line.strip())

        if not match:
            continue

        minutes = int(match.group(1))
        seconds = int(match.group(2))
        fraction = match.group(3) or "0"

        # Convert fraction to milliseconds
        if len(fraction) == 1:
            milliseconds = int(fraction) * 100
        elif len(fraction) == 2:
            milliseconds = int(fraction) * 10
        else:
            milliseconds = int(fraction[:3])

        total_ms = (
            minutes * 60 * 1000
            + seconds * 1000
            + milliseconds
        )

        text = match.group(4).strip()

        if text:
            result.append((total_ms, text))

    return result


def save_lrc(filepath, lrc_text):
    """Save an external LRC file next to the MP3."""

    lrc_path = os.path.splitext(filepath)[0] + ".lrc"

    try:
        with open(lrc_path, "w", encoding="utf-8") as f:
            f.write(lrc_text)

        return lrc_path

    except Exception as e:
        print(f"  Could not save LRC: {e}")
        return None


def embed_lyrics(filepath, plain_lyrics, synced_lyrics):
    """Embed lyrics into the MP3's ID3 tags."""

    try:
        try:
            tags = ID3(filepath)
        except ID3NoHeaderError:
            tags = ID3()

        # -------------------------------------------------
        # Plain lyrics
        # -------------------------------------------------

        if plain_lyrics:

            tags.delall("USLT")

            tags.add(
                USLT(
                    encoding=3,
                    lang="eng",
                    desc="",
                    text=plain_lyrics
                )
            )

        # -------------------------------------------------
        # Synchronized lyrics
        # -------------------------------------------------

        if synced_lyrics:

            synced_lines = parse_lrc(synced_lyrics)

            if synced_lines:

                tags.delall("SYLT")

                tags.add(
                    SYLT(
                        encoding=3,
                        lang="eng",
                        format=2,
                        type=1,
                        desc="",
                        text=synced_lines
                    )
                )

        # Save as ID3v2.4
        tags.save(
            filepath,
            v2_version=4
        )

        return True

    except Exception as e:
        print(f"  Could not embed lyrics: {e}")
        return False


def process_file(filepath):
    """Process one MP3."""

    print()
    print("=" * 60)
    print(filepath)

    info = get_mp3_info(filepath)

    if not info:
        print("  Could not read metadata.")
        return

    print(f"  Artist : {info['artist']}")
    print(f"  Title  : {info['title']}")
    print(f"  Album  : {info['album']}")
    print(f"  Length : {info['duration']} seconds")

    # Artist is still required because we use it to search LRCLIB.
    # Title can now always come from the filename if metadata is missing.
    if not info["artist"]:
        print("  Missing artist metadata. Skipping.")
        return

    if not info["title"]:
        print("  Could not determine title from metadata or filename. Skipping.")
        return

    print("  Searching for lyrics...")

    data = get_lyrics(info)

    if not data:
        print("  ❌ Lyrics not found.")
        return

    plain = data.get("plainLyrics")
    synced = data.get("syncedLyrics")

    if not plain and not synced:
        print("  ❌ No lyrics returned.")
        return

    if plain:
        print("  ✓ Plain lyrics found")

    if synced:
        print("  ✓ Synchronized lyrics found")

    # Save LRC as a compatibility fallback
    if synced:
        lrc_path = save_lrc(filepath, synced)

        if lrc_path:
            print(f"  ✓ Saved: {os.path.basename(lrc_path)}")

    # Embed into MP3
    if embed_lyrics(filepath, plain, synced):
        print("  ✓ Lyrics embedded into MP3")
    else:
        print("  ❌ Failed to embed lyrics")


def find_mp3_files(folder):
    """Find MP3 files recursively."""

    files = []

    for root, dirs, filenames in os.walk(folder):

        for filename in filenames:

            if filename.lower().endswith(".mp3"):
                files.append(os.path.join(root, filename))

    return files


def main():

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python lyrics.py <music_folder>")
        print()
        print("Example:")
        print('  python lyrics.py "C:\\Music"')
        return

    folder = sys.argv[1]

    if not os.path.isdir(folder):
        print(f"Folder does not exist: {folder}")
        return

    files = find_mp3_files(folder)

    if not files:
        print("No MP3 files found.")
        return

    print(f"Found {len(files)} MP3 files.")

    for index, filepath in enumerate(files, start=1):

        print()
        print(f"[{index}/{len(files)}]")

        process_file(filepath)

        # Be polite to the API
        time.sleep(0.5)

    print()
    print("=" * 60)
    print("Finished!")


if __name__ == "__main__":
    main()
