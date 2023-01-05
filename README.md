# esp-config-updater

Update a bunch of ESPEasy devices based on a table.
Something like saltstack or terraform for ESPEasy.

- Input can be an HTML or CSV table.
- Data is grouped into 'islands' to support clever updates.
- Islands can be anywhere in the table, headers are important to find them.
- A unit can be listed in any number of islands.
- Processing goes left to right, top to bottom.
- Logging goes to `./log/`, all changes are logged.

See a sample here:
https://docs.google.com/spreadsheets/d/1UqaxLiMUxmWf_blkbo-72ey-zTr3ZrWkMyTSZqjRu7g/edit?usp=sharing

## How to install

- Clone the repo
- On linux just run `make`
- On other platforms follow the trail in `Makefile`

## How to use

Start `bin/main`:

```
Usage: main [OPTIONS] SOURCE

Options:
  -q, --quiet
  -v, --verbose
  -d, --dryrun    Make no changes
  -f, --failfast  Fail/exit on first failure, otherwise move on the next unit
  -p, --precheck  Connect all mentioned units before updating
  --help          Show this message and exit.
```
