import click
import csv
import json
import logging
import os
import queue
import requests
import threading

from bs4 import BeautifulSoup
from collections import defaultdict
from dataclasses import dataclass
from espcfg.utils import setupLogging
from espcfg.utils import install_hook
from urllib.error import URLError
from zope.testbrowser.browser import Browser

HOME = __file__.rsplit("src", 1)[0]
VAR = os.path.join(HOME, "var")
UNITS_FNAME = os.path.join(VAR, "units.json")

ISLAND_CORNER = "units IP/addr"

LOG = logging.getLogger(__name__)


@dataclass
class Row:
    url: str
    control: str
    value: str


@dataclass
class Island:
    units: list
    urls: dict
    data: list


def readWebTable(url):
    r = requests.get(url)
    if r.status_code != 200:
        LOG.error(f"Request {url} failed: {r.status_code}")
        raise requests.exceptions.RequestException(response=r)

    soup = BeautifulSoup(r.text, features="lxml")
    tbody = soup.find("table").find("tbody")

    table = []
    maxRowLen = 0
    for table_row in tbody.findAll("tr"):
        columns = table_row.findAll("td")
        row = []
        for column in columns:
            text = column.text
            # convert \r \n in the input to real linefeed and newline
            text = text.replace(r"\r", "\r")
            text = text.replace(r"\n", "\n")
            row.append(text)
        if row:
            table.append(row)
            maxRowLen = max(maxRowLen, len(row))
    LOG.info(f"Loaded a table of {len(table)} rows {maxRowLen} columns")
    return table


def readCSV(fname):
    with open(fname, "r") as csv_file:
        csv_reader = csv.reader(csv_file)
        table = [row for row in csv_reader]
    maxRowLen = max([len(row) for row in table])
    LOG.info(f"Loaded a table of {len(table)} rows {maxRowLen} columns")
    return table


def readIslands(table):
    # find the islands
    # be lazy and add an empty row instead of index checking
    table.append([""] * len(table[-1]))
    islands = []
    for ridx, row in enumerate(table):
        for cidx, cell in enumerate(row):
            if cell == ISLAND_CORNER:
                island = loadIsland(table, ridx, cidx)
                islands.append(island)
    LOG.info(f"Loaded {len(islands)} islands")
    return islands


def loadIsland(table, ridx, cidx):
    assert table[ridx][cidx] == ISLAND_CORNER
    roffset = 1

    units = []
    while True:
        unit = table[ridx + roffset][cidx]
        if unit == "URL" and table[ridx + roffset][cidx + 1] == "control name":
            break
        units.append(unit)
        roffset += 1

    header = getIslandRow(table, ridx + roffset, cidx)
    roffset += 1
    assert header.url == "URL"
    assert header.control == "control name"
    assert header.value == "value"

    prevRow = None
    data = []
    urls = defaultdict(list)
    while True:
        row = getIslandRow(table, ridx + roffset, cidx, previous=prevRow)
        if not row.control or row.url == ISLAND_CORNER:
            # an empty row or the next island header is the bottom of the island
            break

        if not row.control.startswith("#"):
            # omit comments
            data.append(row)
            urls[row.url].append(row)

        roffset += 1
        prevRow = row

    island = Island(units=units, data=data, urls=urls)
    return island


def getIslandRow(table, ridx, cidx, previous=None):
    url = table[ridx][cidx]
    control = table[ridx][cidx + 1]
    value = table[ridx][cidx + 2]
    if previous is not None:
        url = url or previous.url
    row = Row(url, control, value)
    return row


class Control:
    def __init__(self, browserControl, form):
        self.browserControl = browserControl
        self.form = form

    def read(self):
        return self.browserControl.value

    def write(self, value):
        self.browserControl.value = value

    def changed(self, newValue):
        prevValue = self.read()
        return prevValue != newValue

    def needPost(self):
        return False


class TextBox(Control):
    pass


class HiddenControl(Control):
    def write(self, value):
        # just ignore writes
        pass

    def changed(self, newValue):
        # must not change hidden controls
        # usually those are previously selected combos with pre-set values
        return False


class CheckBox(Control):
    def read(self):
        return bool(self.browserControl.value)

    def write(self, value):
        value = self._toBool(value)
        self.browserControl.value = value

    def changed(self, newValue):
        prevValue = self.read()
        newValue = self._toBool(newValue)
        return prevValue != newValue

    def _toBool(self, value):
        return value.lower() in ("1", "t", "y")


class Combo(Control):
    # single select combo box support for now

    def read(self):
        return self.browserControl.value[0]

    def write(self, value):
        if value in self.browserControl.options:
            self.browserControl.value = [value]
        elif value in self.browserControl.displayOptions:
            self.browserControl.displayValue = [value]
        else:
            raise ValueError(f"{value} not found in {self.browserControl.name}")

    def changed(self, newValue):
        if newValue in self.browserControl.displayOptions:
            prevValue = self.browserControl.displayValue[0]
            return prevValue != newValue
        prevValue = self.read()
        return prevValue != newValue

    def needPost(self):
        # some controls need to submit the form on change
        # the browser does this with javascript, we need to dig
        soup = BeautifulSoup(self.browserControl.browser.contents, features="lxml")
        elem = soup.find("select", {"name": self.browserControl.name})
        return "onchange" in elem.attrs


class Password(Control):
    def changed(self, newValue):
        # well, ESP never submits passwords
        return True


CTRL_MAPPING = {
    "text": TextBox,
    "search": TextBox,
    "number": TextBox,
    "textarea": TextBox,
    "password": Password,
    "select": Combo,
    "checkbox": CheckBox,
    "hidden": HiddenControl,
}


class Processor:
    dryRun = False
    failFast = False
    name2ip = None

    def __init__(self, dryRun=False, failFast=False):
        self.dryRun = dryRun
        self.failFast = failFast
        self.name2ip = {}

    def process(self, data, host=None):
        for island in data:
            self.processIsland(island, host)

    def processIsland(self, island, host=None):
        for unit in island.units:
            if host is not None and unit!=host:
                continue
            self.processUnit(unit, island)

    def processUnit(self, unit, island):
        LOG.info("Processing %s", unit)
        browser = Browser()
        # optional name -> IP lookup
        unit = self.name2ip.get(unit, unit)
        try:
            unitUrl = f"http://{unit}"
            browser.open(unitUrl)
        except (URLError, OSError) as ex:
            LOG.error("%s %s", unitUrl, ex)
            if self.failFast:
                raise
            return

        for url, rows in island.urls.items():
            pageUrl = f"http://{unit}{url}"
            try:
                browser.open(pageUrl)
            except (URLError, OSError) as ex:
                LOG.error("%s %s", pageUrl, ex)
                if self.failFast:
                    raise
                return
            LOG.info(f"Loaded {pageUrl}")
            form = browser.getForm()
            changed = False
            for row in rows:
                if row.control == '!submit?':
                    if changed:
                        form = self._submitForm(pageUrl, form)
                        changed = False
                    continue
                if row.control == '!submit':
                    form = self._submitForm(pageUrl, form)
                    continue
                if row.control.startswith('!'):
                    # click a button
                    forgiving = False
                    name = row.control[1:]
                    if name.endswith('?'):
                        # don't fail if  the button is not available
                        name = name[:-1]
                        forgiving = True
                    try:
                        browserControl = form.getControl(name=name)
                    except LookupError:
                        if forgiving:
                            continue
                        else:
                            raise

                    msg = f"Clicking {name}"
                    if self.dryRun:
                        msg = f"{msg} this is going to BREAK"
                        LOG.info(msg)
                        continue
                    LOG.info(msg)
                    browserControl.click()
                    form = form.browser.getForm()
                    continue

                try:
                    browserControl = form.getControl(name=row.control)
                except NotImplementedError:
                    LOG.error(f"Control {row.control} not found")
                controlClass = CTRL_MAPPING[browserControl.type]
                control = controlClass(browserControl, form)
                if control.changed(row.value):
                    prev = control.read()
                    control.write(row.value)
                    changed = True
                    LOG.info(f"{row.control} changed from {prev} to {row.value}")
                    if control.needPost():
                        msg = f"{row.control} needs form submission"
                        if self.dryRun:
                            msg = f"{msg} this is going to BREAK"
                        form = self._submitForm(pageUrl, form, msg)
            if changed:
                form = self._submitForm(pageUrl, form)
            else:
                LOG.info(f"{pageUrl} no changes")

    def _submitForm(self, pageUrl, form, msg=None):
        if msg is None:
            msg = f"{pageUrl} form"
        if self.dryRun:
            LOG.info(f"{msg} NOT submitted (dryrun)")
        else:
            form.submit()
            LOG.info(f"{msg} submitted")
            if "resetFlashWriteCounter" in form.browser.contents.decode('utf8'):
                # FS : Daily flash write rate exceeded! (powercycle or send command 'resetFlashWriteCounter' to reset this)
                # so sad, form gets cleared
                # <div class="alert"><span class="closebtn" onclick="this.parentElement.style.display='none';">x</span>FS   : Daily flash write rate exceeded! (powercycle or send command 'resetFlashWriteCounter' to reset this)</div>
                LOG.error("Daily flash write count exceeded!")
            form = form.browser.getForm()
        return form

    def precheck(self, islands, host=None):
        units = set()
        for island in islands:
            for unit in island.units:
                if host is not None and unit!=host:
                    continue
                units.add(unit)

        failed = []
        browser = Browser()
        for unit in sorted(units):
            unit = self.name2ip.get(unit, unit)
            try:
                unitUrl = f"http://{unit}"
                LOG.info("Checking %s", unitUrl)
                browser.open(unitUrl)
            except (URLError, OSError) as ex:
                LOG.error("%s %s", unitUrl, ex)
                failed.append(unit)

        if self.failFast:
            raise SystemExit(1)
    
    def loadUnits(self):
        if os.path.exists(UNITS_FNAME):
            with open(UNITS_FNAME, "r") as inf:
                ulist = json.load(inf)
                self.name2ip = {u[0]: u[1] for u in ulist}


@click.command()
@click.argument("source")
@click.option("--quiet", "-q", default=False, is_flag=True)
@click.option("--verbose", "-v", default=False, is_flag=True)
@click.option("--dryrun", "-d", default=False, is_flag=True, help="Make no changes")
@click.option(
    "--failfast",
    "-f",
    default=False,
    is_flag=True,
    help="Fail/exit on first failure, otherwise move on the next unit",
)
@click.option(
    "--precheck",
    "-p",
    default=False,
    is_flag=True,
    help="Connect all mentioned units before updating",
)
@click.option("--host", "-h", default=None, help="Process only the given host")
def config(source, quiet, verbose, dryrun, failfast, precheck, host):
    os.chdir(HOME)

    level = logging.INFO
    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG

    install_hook()
    setupLogging("log/config.log", stdout=True, level=level)

    if source.lower().startswith("http"):
        data = readWebTable(source)
    elif source.lower().endswith(".csv"):
        data = readCSV(source)
    islands = readIslands(data)
    if dryrun:
        LOG.info("-----------------------------")
        LOG.info("-- DRY RUN ------------------")
        LOG.info("-----------------------------")
    p = Processor(dryRun=dryrun, failFast=failfast)
    p.loadUnits()
    if precheck:
        p.precheck(islands, host)
    p.process(islands, host)


class Discovery:
    def _getUnitName(self, data):
        try:
            unitName = data["System"]["Unit Name"]
        except KeyError:
            pass
        try:
            unitIP = data["WiFi"]["IP Address"]
        except KeyError:
            pass
        unitName = unitName or unitIP
        unitName = unitName.replace(".", "_")
        return unitName

    def worker(self, unitip, collector, timeout=1):
        url = f"http://{unitip}/json"
        LOG.debug("Trying %s", url)
        try:
            # well, attack directly, any ping+whatnot would just add more time
            r = requests.get(url, timeout=timeout)
        except Exception as exc:
            LOG.debug("%s request error %s", url, exc)
            return
        if r.status_code != 200:
            LOG.debug("%s request response %s", url, r.status_code)
            return
        try:
            data = r.json()
        except Exception as exc:
            LOG.debug("%s JSON conversion error %s", url, exc)
            return
        collector.put(data)
        unitName = self._getUnitName(data)
        varFile = os.path.join(VAR, f"{unitName}.json")
        with open(varFile, "w") as of:
            of.write(r.text)

    def _loadCollector(self, collector):
        units = {}
        while True:
            try:
                unit = collector.get_nowait()
                unitName = self._getUnitName(unit)
                units[unitName] = unit
            except queue.Empty:
                break
        return units

    def discoverUnitsInThread(self, iplist, timeout=1):
        collector = queue.Queue()
        threads = []

        for ip in iplist:
            thr = threading.Thread(target=self.worker, args=(ip, collector, timeout))
            threads.append(thr)
            thr.start()

        for thr in threads:
            thr.join()

        units = self._loadCollector(collector)
        return units

    def discoverUnits(self, iprange, timeout=1):
        LOG.info("Running discovery on %s", iprange)
        parts = iprange.split(".")
        base = ".".join(parts[0:3])

        iplist = [f"{base}.{last}" for last in range(1, 254)]
        units = self.discoverUnitsInThread(iplist)
        LOG.info("Discovered %s units", len(units))

        # collect nodes from units, looks like they don't always respond directly
        iplist = []
        for unit in units.values():
            for unode in unit["nodes"]:
                if unode["name"] not in units:
                    iplist.append(unode["ip"])

        extra = self.discoverUnitsInThread(iplist, timeout=timeout * 3)
        if extra:
            LOG.info("Discovered %s units via unit nodes", len(extra))
            units.update(extra)

        return units


@click.command()
@click.argument("iprange")
# An IP range given as 192.168.0.1, the whole 192.168.0.1/24 will be scanned
@click.option("--quiet", "-q", default=False, is_flag=True)
@click.option("--verbose", "-v", default=False, is_flag=True)
@click.option("--timeout", "-t", default=1, type=int)
def discover(iprange, quiet, verbose, timeout):
    level = logging.INFO
    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG

    install_hook()
    setupLogging("log/discover.log", stdout=True, level=level)

    units = Discovery().discoverUnits(iprange, timeout=timeout)
    ulist = []
    for unitName, unit in sorted(units.items()):
        unitIP = unit["WiFi"]["IP Address"]
        ulist.append([unitName, unitIP])
        LOG.info("%s %s", unitName, unitIP)

    with open(UNITS_FNAME, "w") as of:
        json.dump(ulist, of, indent=4)
