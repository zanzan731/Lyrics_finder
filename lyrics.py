import os
import re
import sys
import time
import requests

from difflib import SequenceMatcher

from mutagen.mp3 import MP3
from mutagen.id3 import ID3, USLT, SYLT, ID3NoHeaderError


# ============================================================
# CONFIGURATION
# ============================================================

LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"

USER_AGENT = (
    "MP3LyricsEmbedder/2.0 "
    "(https://lrclib.net/)"
)

# The report is always created next to this Python script.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_FILE = os.path.join(SCRIPT_DIR, "lyrics_report.txt")


# ============================================================
# GENERAL HELPERS
# ============================================================

def normalize_text(text):
    """
    Normalize text for comparing artists/titles.

    This makes comparisons less sensitive to:
        - capitalization
        - punctuation
        - extra spaces
        - accents
    """

    if not text:
        return ""

    text = str(text).strip().lower()

    # Normalize common separators
    text = text.replace("&", " and ")

    # Remove accents
    import unicodedata

    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    # Remove punctuation
    text = re.sub(r"[^a-z0-9\s]", " ", text)

    # Collapse spaces
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def similarity(a, b):
    """Return similarity between two strings from 0.0 to 1.0."""

    a = normalize_text(a)
    b = normalize_text(b)

    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    return SequenceMatcher(None, a, b).ratio()


def clean_filename(name):
    """Make a filename safe for Windows/Linux/macOS."""
    return re.sub(r'[<>:"/\\|?*]', "_", name)


# ============================================================
# TITLE DETECTION
# ============================================================

def title_from_filename(filepath, artist=None):
    """
    Determine a title from the filename.

    Examples:

        Crashing Hard.mp3
            -> Crashing Hard

        Crashing Hard...mp3
            -> Crashing Hard

        Crashing Hard...2.mp3
            -> Crashing Hard

        Crashing Hard (2).mp3
            -> Crashing Hard

        Kygo - Freeze.mp3
            -> Freeze

        ABBA(Mamma mia!).mp3
            -> Mamma mia!

        ABBA - Mamma Mia.mp3
            -> Mamma Mia
    """

    filename = os.path.basename(filepath)

    # Remove extension
    title = os.path.splitext(filename)[0].strip()

    # --------------------------------------------------------
    # Remove duplicate suffixes
    # --------------------------------------------------------

    # Song...2
    title = re.sub(
        r"\s*\.{2,}\s*[2-9]\d*\s*$",
        "",
        title
    )

    # Song...
    title = re.sub(
        r"\s*\.{2,}\s*$",
        "",
        title
    )

    # Song (2)
    # Song [2]
    # Song {2}
    # Song - 2
    # Song _ 2
    title = re.sub(
        r"""
        \s*
        (?:
            \(\s*[2-9]\d*\s*\)
            |
            \[\s*[2-9]\d*\s*\]
            |
            \{\s*[2-9]\d*\s*\}
            |
            [-_]\s*[2-9]\d*
        )
        \s*$
        """,
        "",
        title,
        flags=re.VERBOSE
    )

    title = title.strip()

    # --------------------------------------------------------
    # If artist is known, detect:
    #
    # ABBA(Mamma Mia!)
    # ABBA - Mamma Mia
    # --------------------------------------------------------

    if artist:
        artist_clean = artist.strip()

        # ABBA(Mamma Mia!)
        pattern = re.compile(
            r"^\s*" + re.escape(artist_clean) + r"\s*\((.+)\)\s*$",
            re.IGNORECASE
        )

        match = pattern.match(title)

        if match:
            extracted = match.group(1).strip()

            if extracted:
                return extracted

        # ABBA[Mamma Mia!]
        pattern = re.compile(
            r"^\s*" + re.escape(artist_clean) + r"\s*\[(.+)\]\s*$",
            re.IGNORECASE
        )

        match = pattern.match(title)

        if match:
            extracted = match.group(1).strip()

            if extracted:
                return extracted

        # ABBA - Mamma Mia
        prefix = artist_clean.lower()

        if title.lower().startswith(prefix + " - "):
            extracted = title[len(artist_clean) + 3:].strip()

            if extracted:
                return extracted

    # --------------------------------------------------------
    # Generic "Artist - Title" format
    #
    # Only split if there is one clear separator.
    # --------------------------------------------------------

    if " - " in title:

        parts = title.split(" - ", 1)

        if len(parts) == 2:
            left = parts[0].strip()
            right = parts[1].strip()

            if left and right:
                # If metadata artist exists and matches left,
                # definitely use the right side.
                if artist and similarity(left, artist) >= 0.75:
                    return right

    return title.strip()


# ============================================================
# MP3 INFORMATION
# ============================================================

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
                value = str(value[0]).strip()

                if value:
                    return value

            return None

        artist = get_tag("TPE1")
        album = get_tag("TALB")
        metadata_title = get_tag("TIT2")

        # ----------------------------------------------------
        # Title handling
        # ----------------------------------------------------

        if metadata_title:

            # Sometimes metadata itself contains:
            #
            # ABBA(Mamma Mia!)
            #
            # so run it through filename-style parsing too.
            fake_path = metadata_title + ".mp3"

            title = title_from_filename(
                fake_path,
                artist
            )

        else:
            # No title metadata -> use filename
            title = title_from_filename(
                filepath,
                artist
            )

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


# ============================================================
# LRCLIB SEARCH
# ============================================================

def search_lrclib(info):
    """
    Search LRCLIB without relying on MP3 duration.

    Duration is intentionally NOT used as a requirement because
    the user's MP3 files may have been cut/edited.
    """

    artist = info["artist"]
    title = info["title"]

    headers = {
        "User-Agent": USER_AGENT
    }

    # --------------------------------------------------------
    # Search attempts
    #
    # Start precise and then become slightly broader.
    # --------------------------------------------------------

    attempts = [
        {
            "track_name": title,
            "artist_name": artist,
        },

        {
            "track_name": title,
        },

        {
            "q": f"{artist} {title}",
        },
    ]

    all_results = []
    seen_ids = set()

    for attempt_number, params in enumerate(attempts, start=1):

        try:

            response = requests.get(
                LRCLIB_SEARCH_URL,
                params=params,
                headers=headers,
                timeout=15
            )

            if response.status_code == 429:

                retry_after = response.headers.get(
                    "Retry-After",
                    "5"
                )

                try:
                    retry_seconds = float(retry_after)
                except ValueError:
                    retry_seconds = 5

                print(
                    f"  Rate limited. Waiting "
                    f"{retry_seconds:g} seconds..."
                )

                time.sleep(retry_seconds)

                # Retry this exact request
                response = requests.get(
                    LRCLIB_SEARCH_URL,
                    params=params,
                    headers=headers,
                    timeout=15
                )

            response.raise_for_status()

            data = response.json()

            if not isinstance(data, list):
                continue

            for result in data:

                result_id = result.get("id")

                if result_id in seen_ids:
                    continue

                seen_ids.add(result_id)
                all_results.append(result)

            # If the precise artist/title search returned
            # something, normally we have enough candidates.
            if attempt_number == 1 and data:
                break

        except requests.RequestException as e:

            print(
                f"  Search attempt {attempt_number} "
                f"failed: {e}"
            )

        except Exception as e:

            print(
                f"  Search attempt {attempt_number} "
                f"failed: {e}"
            )

    if not all_results:
        return None

    # --------------------------------------------------------
    # Score all candidates.
    # --------------------------------------------------------

    candidates = []

    for result in all_results:

        result_artist = result.get("artistName") or ""
        result_title = result.get("trackName") or ""

        if not result_title:
            continue

        artist_score = similarity(
            artist,
            result_artist
        )

        title_score = similarity(
            title,
            result_title
        )

        # Exact artist/title matches should win strongly.
        score = (
            artist_score * 0.55
            + title_score * 0.45
        )

        # ----------------------------------------------------
        # Duration is ONLY a tie-breaker.
        #
        # It does NOT reject the result.
        # ----------------------------------------------------

        result_duration = result.get("duration")

        if result_duration:

            try:
                difference = abs(
                    float(info["duration"])
                    - float(result_duration)
                )

                # Small bonus for similar durations.
                if difference <= 5:
                    score += 0.08
                elif difference <= 15:
                    score += 0.03

            except (ValueError, TypeError):
                pass

        # Prefer synchronized lyrics.
        if result.get("syncedLyrics"):
            score += 0.05
        elif result.get("plainLyrics"):
            score += 0.01

        candidates.append(
            (score, result)
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item[0],
        reverse=True
    )

    best_score, best_result = candidates[0]

    best_artist = best_result.get(
        "artistName",
        ""
    )

    best_title = best_result.get(
        "trackName",
        ""
    )

    # --------------------------------------------------------
    # Safety check.
    #
    # Don't accept a completely unrelated song just because
    # LRCLIB returned it from a broad search.
    # --------------------------------------------------------

    artist_score = similarity(
        artist,
        best_artist
    )

    title_score = similarity(
        title,
        best_title
    )

    if artist_score < 0.55 or title_score < 0.55:
        return None

    print(
        f"  ✓ Match: {best_artist} - {best_title}"
    )

    if best_result.get("syncedLyrics"):
        print("  ✓ Synchronized lyrics available")
    elif best_result.get("plainLyrics"):
        print("  ✓ Plain lyrics available")

    return best_result


# ============================================================
# LRC PARSING
# ============================================================

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
            result.append(
                (total_ms, text)
            )

    return result


# ============================================================
# LRC FILE
# ============================================================

def get_lrc_path(filepath):
    """Return the external LRC path for an MP3."""

    return os.path.splitext(filepath)[0] + ".lrc"


def has_lrc_file(filepath):
    """Check whether this MP3 already has an LRC file."""

    return os.path.isfile(
        get_lrc_path(filepath)
    )


def save_lrc(filepath, lrc_text):
    """Save an external LRC file next to the MP3."""

    lrc_path = get_lrc_path(filepath)

    try:

        with open(
            lrc_path,
            "w",
            encoding="utf-8"
        ) as f:
            f.write(lrc_text)

        return lrc_path

    except Exception as e:

        print(
            f"  Could not save LRC: {e}"
        )

        return None


# ============================================================
# EMBEDDING
# ============================================================

def embed_lyrics(
    filepath,
    plain_lyrics,
    synced_lyrics
):
    """Embed lyrics into the MP3's ID3 tags."""

    try:

        try:
            tags = ID3(filepath)

        except ID3NoHeaderError:
            tags = ID3()

        # ----------------------------------------------------
        # Plain lyrics
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Synchronized lyrics
        # ----------------------------------------------------

        if synced_lyrics:

            synced_lines = parse_lrc(
                synced_lyrics
            )

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

        tags.save(
            filepath,
            v2_version=4
        )

        return True

    except Exception as e:

        print(
            f"  Could not embed lyrics: {e}"
        )

        return False


# ============================================================
# REPORT
# ============================================================

def write_report(
    found,
    skipped,
    missing
):
    """
    Write a report next to this Python script.

    The report is replaced after every run so it always
    represents the current state of the library.
    """

    try:

        with open(
            REPORT_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            f.write("=" * 70 + "\n")
            f.write("LYRICS REPORT\n")
            f.write("=" * 70 + "\n\n")

            # ------------------------------------------------
            # Found
            # ------------------------------------------------

            f.write(
                f"NEW LYRICS FOUND: {len(found)}\n"
            )
            f.write("-" * 70 + "\n")

            if found:

                for item in found:

                    f.write(
                        f"{item['file']}\n"
                    )

                    f.write(
                        f"  Search: "
                        f"{item['artist']} - "
                        f"{item['title']}\n"
                    )

                    f.write(
                        f"  Match: "
                        f"{item['match_artist']} - "
                        f"{item['match_title']}\n"
                    )

                    f.write(
                        f"  Lyrics: "
                        f"{item['lyrics_type']}\n\n"
                    )

            else:
                f.write("None\n\n")

            # ------------------------------------------------
            # Skipped
            # ------------------------------------------------

            f.write(
                f"ALREADY HAD LRC: {len(skipped)}\n"
            )
            f.write("-" * 70 + "\n")

            if skipped:

                for filepath in skipped:
                    f.write(
                        f"{filepath}\n"
                    )

            else:
                f.write("None\n")

            f.write("\n")

            # ------------------------------------------------
            # Missing
            # ------------------------------------------------

            f.write(
                f"STILL WITHOUT LYRICS: {len(missing)}\n"
            )
            f.write("-" * 70 + "\n")

            if missing:

                for item in missing:

                    f.write(
                        f"{item['file']}\n"
                    )

                    f.write(
                        f"  Artist: "
                        f"{item['artist']}\n"
                    )

                    f.write(
                        f"  Title: "
                        f"{item['title']}\n"
                    )

                    f.write("\n")

            else:
                f.write(
                    "Every MP3 has lyrics!\n"
                )

            f.write("\n")
            f.write("=" * 70 + "\n")

        print()
        print(
            f"Report written to: {REPORT_FILE}"
        )

    except Exception as e:

        print(
            f"Could not write report: {e}"
        )


# ============================================================
# PROCESS ONE FILE
# ============================================================

def process_file(filepath):
    """
    Process one MP3.

    Returns:
        ("found", report_data)
        ("skipped", filepath)
        ("missing", report_data)
    """

    print()
    print("=" * 60)
    print(filepath)

    # --------------------------------------------------------
    # Skip if LRC already exists
    # --------------------------------------------------------

    if has_lrc_file(filepath):

        print(
            "  ✓ LRC file already exists. Skipping."
        )

        return (
            "skipped",
            filepath
        )

    # --------------------------------------------------------
    # Read MP3
    # --------------------------------------------------------

    info = get_mp3_info(filepath)

    if not info:

        print(
            "  Could not read metadata."
        )

        return (
            "missing",
            {
                "file": filepath,
                "artist": "",
                "title": "",
            }
        )

    print(
        f"  Artist : {info['artist']}"
    )

    print(
        f"  Title  : {info['title']}"
    )

    print(
        f"  Album  : {info['album']}"
    )

    print(
        f"  Length : {info['duration']} seconds"
    )

    # --------------------------------------------------------
    # Artist is still necessary.
    # --------------------------------------------------------

    if not info["artist"]:

        print(
            "  ❌ Missing artist metadata. "
            "Cannot safely search."
        )

        return (
            "missing",
            {
                "file": filepath,
                "artist": "",
                "title": info["title"] or "",
            }
        )

    if not info["title"]:

        print(
            "  ❌ Could not determine title."
        )

        return (
            "missing",
            {
                "file": filepath,
                "artist": info["artist"],
                "title": "",
            }
        )

    # --------------------------------------------------------
    # Search
    # --------------------------------------------------------

    print(
        "  Searching LRCLIB..."
    )

    data = search_lrclib(info)

    if not data:

        print(
            "  ❌ Lyrics not found."
        )

        return (
            "missing",
            {
                "file": filepath,
                "artist": info["artist"],
                "title": info["title"],
            }
        )

    plain = data.get(
        "plainLyrics"
    )

    synced = data.get(
        "syncedLyrics"
    )

    if not plain and not synced:

        print(
            "  ❌ Match found, "
            "but no lyrics available."
        )

        return (
            "missing",
            {
                "file": filepath,
                "artist": info["artist"],
                "title": info["title"],
            }
        )

    # --------------------------------------------------------
    # Save LRC
    # --------------------------------------------------------

    lrc_text = synced or plain

    lrc_path = save_lrc(
        filepath,
        lrc_text
    )

    if not lrc_path:

        print(
            "  ❌ Could not create LRC file."
        )

        return (
            "missing",
            {
                "file": filepath,
                "artist": info["artist"],
                "title": info["title"],
            }
        )

    print(
        f"  ✓ Saved: "
        f"{os.path.basename(lrc_path)}"
    )

    # --------------------------------------------------------
    # Embed lyrics
    # --------------------------------------------------------

    if embed_lyrics(
        filepath,
        plain,
        synced
    ):

        print(
            "  ✓ Lyrics embedded into MP3"
        )

    else:

        print(
            "  ⚠ LRC saved, "
            "but embedding failed."
        )

    # --------------------------------------------------------
    # Report
    # --------------------------------------------------------

    if synced:
        lyrics_type = "synchronized + plain"
    else:
        lyrics_type = "plain only"

    return (
        "found",
        {
            "file": filepath,
            "artist": info["artist"],
            "title": info["title"],
            "match_artist": data.get(
                "artistName",
                ""
            ),
            "match_title": data.get(
                "trackName",
                ""
            ),
            "lyrics_type": lyrics_type,
        }
    )


# ============================================================
# FIND MP3 FILES
# ============================================================

def find_mp3_files(folder):
    """Find MP3 files recursively."""

    files = []

    for root, dirs, filenames in os.walk(folder):

        for filename in filenames:

            if filename.lower().endswith(
                ".mp3"
            ):

                files.append(
                    os.path.join(
                        root,
                        filename
                    )
                )

    files.sort(
        key=lambda x: x.lower()
    )

    return files


# ============================================================
# MAIN
# ============================================================

def main():

    if len(sys.argv) < 2:

        print("Usage:")
        print(
            '  python lyrics.py "C:\\Music"'
        )

        return

    folder = sys.argv[1]

    if not os.path.isdir(folder):

        print(
            f"Folder does not exist: {folder}"
        )

        return

    files = find_mp3_files(
        folder
    )

    if not files:

        print(
            "No MP3 files found."
        )

        return

    print(
        f"Found {len(files)} MP3 files."
    )

    found = []
    skipped = []
    missing = []

    for index, filepath in enumerate(
        files,
        start=1
    ):

        print()
        print(
            f"[{index}/{len(files)}]"
        )

        status, data = process_file(
            filepath
        )

        if status == "found":

            found.append(data)

        elif status == "skipped":

            skipped.append(data)

        elif status == "missing":

            missing.append(data)

        # LRCLIB asks clients to make sequential requests
        # and use a short delay between requests.
        time.sleep(0.5)

    # --------------------------------------------------------
    # Final report
    # --------------------------------------------------------

    write_report(
        found,
        skipped,
        missing
    )

    # --------------------------------------------------------
    # Console summary
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("FINISHED")
    print("=" * 60)

    print(
        f"New lyrics found : {len(found)}"
    )

    print(
        f"Already had LRC  : {len(skipped)}"
    )

    print(
        f"Still missing    : {len(missing)}"
    )

    if missing:

        print()
        print(
            "Files still without lyrics:"
        )

        for item in missing:

            print(
                f"  - {os.path.basename(item['file'])}"
            )

    print()
    print(
        f"Full report: {REPORT_FILE}"
    )


if __name__ == "__main__":
    main()
