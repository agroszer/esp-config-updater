import click
import csv
import logging
import os
import requests

from bs4 import BeautifulSoup
from collections import defaultdict
from dataclasses import dataclass
from espcfg.utils import setupLogging
from espcfg.utils import install_hook
from urllib.error import URLError
from zope.testbrowser.browser import Browser

HOME = __file__.rsplit("src", 1)[0]

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
    for table_row in tbody.findAll("tr"):
        columns = table_row.findAll("td")
        row = []
        for column in columns:
            row.append(column.text)
        if row:
            table.append(row)
    return table


def readCSV(fname):
    with open(fname, "r") as csv_file:
        csv_reader = csv.reader(csv_file)
        table = [row for row in csv_reader]
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
        if not row.control or not row.value or row.url == ISLAND_CORNER:
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
}


class Processor:
    dryRun = False
    failFast = False

    def __init__(self, dryRun=False, failFast=False):
        self.dryRun = dryRun
        self.failFast = failFast

    def process(self, data):
        for island in data:
            self.processIsland(island)

    def processIsland(self, island):
        for unit in island.units:
            self.processUnit(unit, island)

    def processUnit(self, unit, island):
        browser = Browser()
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
                browserControl = form.getControl(name=row.control)
                controlClass = CTRL_MAPPING[browserControl.type]
                control = controlClass(browserControl, form)
                if control.changed(row.value):
                    prev = control.read()
                    control.write(row.value)
                    changed = True
                    LOG.info(f"{row.control} changed from {prev} to {row.value}")
                    if control.needPost():
                        LOG.info(f"{row.control} needs form submission")
                        form.submit()
                        form = browser.getForm()
            if changed:
                if self.dryRun:
                    LOG.info(f"{pageUrl} form NOT submitted (dryrun)")
                else:
                    LOG.info(f"{pageUrl} form submitted")
                    form.submit()
            else:
                LOG.info(f"{pageUrl} no changes")

    def precheck(self, islands):
        units = set()
        for island in islands:
            for unit in island.units:
                units.add(unit)

        failed = []
        browser = Browser()
        for unit in sorted(units):
            try:
                unitUrl = f"http://{unit}"
                LOG.info("Checking %s", unitUrl)
                browser.open(unitUrl)
            except (URLError, OSError) as ex:
                LOG.error("%s %s", unitUrl, ex)
                failed.append(unit)

        if self.failFast:
            raise SystemExit(1)


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
def main(source, quiet, verbose, dryrun, failfast, precheck):
    os.chdir(HOME)

    level = logging.INFO
    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG

    install_hook()
    setupLogging("log/main.log", stdout=True, level=level)

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
    if precheck:
        p.precheck(islands)
    p.process(islands)
