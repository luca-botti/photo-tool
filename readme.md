# Various tools to manage files easily

This repo containes various tools to manage files and photos, so that you can easily manage your files.

## check and move

given a source directory, a destination directory and a todo and done folder it will:

- scan the destination folder to have in memory all the files that are present in the destination (just the list)
- check each file in the source folder, if it is present in the destination folder (byte comparison) it will be put in the done folder, otherwise in the todo folder

### Usage

```bash
python .\check_and_move.py "F:\other-data" \\192.168.178.128\remote-data F:\doc\todo F:\doc\done  
```

## file organizer

scan a source folder for extensions and normally recocnised junk files. then enable the user to define a folder for each extension.
it has a GUI.

## Photo and Video Management Tool

it will organize the photos and videos from a specified folder in this way:

- With location: `{year}/{month}-{year}/{month}-{year}-{location}/{year}-{month}-{day}T{hour}-{minute}-{second}_{location}_[camera].{ext}`
- Without location: `{year}/{month}-{year}/{year}{month}{day}T{hour}-{minute}-{second}_[camera].{ext}`

Use `--help` to see the options.

> At the moment the move functionality does not work

### Install

(If you can't install requirements globally use this)

```bash
python -m venv <your-venv-name> --without-pip --system-site-packages
source yourenv/bin/activate
curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py
python get-pip.py
rm get-pip.py
```

Then use `pip install -r requirements.txt`.

### Usage

```bash
python .\photo-rename-tool.py <source-path> <destination-path>
```

Use:

```bash
python .\photo-rename-tool.py --help
```

To see other options.

> WARNING: if you do not use the --offline flag, it will try to geotag the photos and videos using an online API. This API have a rate limiter of 1 request every 2 seconds, so for large batches of files it will take a lot of times.
