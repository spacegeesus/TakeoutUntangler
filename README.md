# Takeout Untangler

Leaving Google Photos? Google's export (Takeout) gives you everything, but as a mess: wrong dates, photos misfiled into the wrong year, every album duplicated, trash mixed in, and a `.json` file next to every picture. This free tool turns that into a clean library:

```
From Google Photos\
  2019\2019-07\IMG_1853.jpg        <- real taken date set on every file
  2024\2024-11\20241109_160813.jpg
  Albums\Train\...                 <- copies of your albums
  _Trash\                          <- what was in Google's trash, kept apart
  untangle-log.csv                 <- where every file went and why
```

Nothing is uploaded anywhere. It runs on your own computer, reads the zip files directly (no need to unzip 50 GB first), and leaves the originals untouched. MIT license.

## Your move

1. 🔴 **Export from Google.** Go to takeout.google.com → **Deselect all** → tick only **Google Photos** → Next → file type **.zip**, size **50 GB** → **Create export**. Google emails you when it's ready, usually within a day. Download every zip it gives you into one folder.
2. 🔴 **Install Python** if you don't have it: python.org → Downloads → run the installer and **tick "Add python.exe to PATH"** on the first screen.
3. 🔴 **Get this tool.** Click the green **Code** button above → **Download ZIP** → unzip it anywhere.
4. 🔴 **Run it.** Easiest: drag your Takeout zip (or the folder holding several zips) onto `untangle-dragdrop.bat`. A window opens, shows progress, and your library appears in `Pictures\From Google Photos`.

   Or from a command prompt, which lets you pick the output folder:

   ```
   python untangle.py "C:\Downloads\takeout-001.zip" "C:\Downloads\takeout-002.zip" --out "D:\My Photos"
   ```

   Add `--dry-run` to see the counts without writing anything.
5. 🔴 **Check the result,** then back it up somewhere second. Only after that turn off backup in the Google Photos app on your phone and clear the cloud at photos.google.com. Never delete in the app first: that deletes your phone copies too.

## What good looks like

The summary at the end says how many files were placed, how many dates came from Google's sidecar files (should be nearly all), and `no date found` should be 0 or close to it. Anything undated goes to an `Unknown date` folder rather than being guessed.

## Options

| Flag | What it does |
|---|---|
| `--dry-run` | Count and report only, write nothing |
| `--no-albums` | Skip the `Albums` copies (every album photo is in its year folder anyway) |
| `--skip-trash` | Leave Google's trash out entirely |
| `--no-exif` | Only set file dates, don't write dates into JPEG EXIF |

Writing EXIF dates needs one extra package: `pip install piexif`. Without it the tool still works and just sets file dates.

## What it handles

- Dates from Google's `photoTakenTime`, falling back to EXIF, then the filename.
- Sidecar name quirks: truncated long names, `(1)` numbering, `-edited` copies, Samsung `(0)` burst names, motion-photo video halves (`.MP4` / `.MP` that share the photo's sidecar).
- Duplicates by content, so a photo in three albums becomes one file plus album copies.
- GPS from the sidecar written into JPEGs that lack it.
- Several zips at once, or an already-unzipped Takeout folder.
