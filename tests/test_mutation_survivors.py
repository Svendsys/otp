"""Tests for the mutations a second audit round found still surviving.

The first round's blind spot was the printed page, and test_pad_contents.py
closed it. This round's is everything either side of that page: the argv
handed to `lp`, the geometry the paper setting is supposed to produce, the
guards that keep a hand-edited config or a mistyped flag from printing a
useless pad, and the navigation that decides whether an operator can get
back to the menu.

The shape of the gap was consistent. Most of these paths had a test that
touched them and asserted something weaker than the thing that matters:
crop marks were counted by looking for four line segments on a page that
draws eight, a font size too wide for the page was caught by a height check
that happened to reject it too, and --training was verified by reading the
log line rather than the watermark. Each assertion here is chosen to fail
for the one reason it names.
"""
import collections
import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import otp_generator as g
from otpunit import codewords as cw
from otpunit import config, jobs, printer, ui
from otpunit.hw.buttons import FakeButtons, Press
from otpunit.hw.display import FakeDisplay, Frame

SCRIPT = str(REPO / "otp_generator.py")


# --- helpers ------------------------------------------------------------


def cli(args, cwd):
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=cwd,
                          capture_output=True)


def glyph_rows(data: bytes, page: int = 0) -> list[str]:
    """Text rows of a rendered page, reconstructed from glyph coordinates."""
    pypdf = pytest.importorskip("pypdf")
    cells = []

    def visit(text, cm, tm, font_dict, font_size):
        stripped = text.strip()
        if stripped:
            cells.append((round(tm[5], 1), tm[4], stripped))

    pypdf.PdfReader(io.BytesIO(data)).pages[page].extract_text(visitor_text=visit)
    rows = collections.defaultdict(list)
    for y, x, s in cells:
        rows[y].append((x, s))
    return ["".join(s for _, s in sorted(items))
            for _, items in sorted(rows.items(), key=lambda kv: -kv[0])]


def page_text(data: bytes, page: int = 0) -> str:
    return "\n".join(glyph_rows(data, page))


def content_stream(data: bytes, page: int = 0) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    return pypdf.PdfReader(io.BytesIO(data)).pages[page].get_contents().get_data()


def line_segments(content: bytes) -> list[tuple]:
    """Every straight line drawn on the page, as (x1, y1, x2, y2)."""
    return [tuple(round(float(v), 1) for v in seg)
            for seg in re.findall(rb"([\d.]+) ([\d.]+) m ([\d.]+) ([\d.]+) l",
                                  content)]


def rectangles(content: bytes) -> list[tuple]:
    """Every rectangle drawn on the page, as (x, y, width, height)."""
    return [tuple(round(float(v), 1) for v in rect)
            for rect in re.findall(rb"([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+) re",
                                   content)]


def pdf_pages(data: bytes):
    pypdf = pytest.importorskip("pypdf")
    return pypdf.PdfReader(io.BytesIO(data)).pages


def training_marks(data: bytes) -> int:
    """How many times the page says TRAINING: the watermark and the footer."""
    return page_text(data).count("TRAINING")


def pad_pdf(**settings) -> bytes:
    """A pad pair rendered exactly as the unit would render it."""
    spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "RUSTED-BADGER",
                        config.Settings(**settings))
    return bytes(jobs.generate(spec, progress=lambda d, t: None))


def tiny_vocabulary(tmp_path) -> str:
    """A three-by-three vocabulary: nine codewords exist in total."""
    (tmp_path / "modifiers.txt").write_text("ALPHA\nBRAVO\nCHARLIE\n")
    nouns = tmp_path / "nouns"
    nouns.mkdir()
    (nouns / "things.txt").write_text("ANVIL\nBEACON\nCANDLE\n")
    return str(tmp_path)


class FakeCups(printer.Cups):
    """A printer that records what it was asked to do."""

    def __init__(self, devices=(("usb://Fake/Laser?serial=1", "Fake Laser"),)):
        super().__init__(run=None)
        self._devices = [printer.Device(u, d) for u, d in devices]
        self.submitted = []
        self.purged = 0
        self.tried = []

    def devices(self):
        return list(self._devices)

    def ensure_queue(self, device, name="OTP"):
        self.tried.append(device.uri)
        return name

    def submit(self, data, name="OTP", title="OTP", options=None):
        self.submitted.append({"title": title, "data": bytes(data),
                               "options": options})
        return f"job-{len(self.submitted)}"

    def active_jobs(self, name="OTP"):
        return 0

    def purge(self, name="OTP"):
        self.purged += 1


def make_app(script, cups=None, settings=None):
    return ui.App(
        display=FakeDisplay(),
        buttons=FakeButtons(script),
        cups=cups or FakeCups(),
        settings=settings or config.Settings(pages=2),
        vocabulary=cw.Vocabulary(),
        config_path="/nonexistent",
        poll_seconds=0,
    )


def recording_run(result_stdout=b"request id is OTP-3 (1 file)"):
    """A Cups._run that records argv and stdin instead of shelling out."""
    seen = {}

    class Result:
        returncode = 0
        stdout = result_stdout
        stderr = b""

    def run(argv, stdin=None):
        seen["argv"] = list(argv)
        seen["stdin"] = stdin
        return Result()

    return seen, run


def exploding_cups():
    """A Cups whose every call raises, as a wedged cupsd's would."""
    def boom(argv, stdin=None):
        raise TimeoutError("cupsd wedged")
    return printer.Cups(run=boom)


# --- what actually reaches lp -------------------------------------------


class TestTheArgvHandedToLp:
    """
    Everything the printer is told about a job travels in this one argv.
    Nothing downstream of it is under our control, so a dropped argument is
    invisible until a pad comes out on the wrong paper.
    """

    def test_the_title_and_every_option_reach_lp(self):
        seen, run = recording_run()
        job = printer.Cups(run=run).submit(bytearray(b"%PDF-fake"), title="OTP A",
                                           options={"media": "Letter"})
        argv = seen["argv"]

        assert job == "OTP-3"
        # The title is the IPP job-name. It is deliberately not the codeword,
        # but it still has to be set: it is how the operator tells copy A
        # from copy B in the queue, and how the panel's labelling matches
        # what CUPS shows.
        assert "-t" in argv, "the job title never reached lp"
        assert argv[argv.index("-t") + 1] == "OTP A"
        # Without the media option lp falls back to the queue default, so a
        # Letter tray would be asked for A4 -- the sheet the imposed cut
        # lines were calculated for is not the sheet that comes out.
        assert "-o" in argv, "the media option never reached lp"
        assert argv[argv.index("-o") + 1] == "media=Letter"
        assert seen["stdin"] == b"%PDF-fake", "key material goes over stdin"

    def test_every_option_is_passed_not_just_the_first(self):
        seen, run = recording_run()
        printer.Cups(run=run).submit(bytearray(b"x"),
                                     options={"media": "A4", "sides": "one-sided"})
        argv = seen["argv"]
        assert argv.count("-o") == 2
        assert "media=A4" in argv and "sides=one-sided" in argv

    def test_every_cups_call_is_bounded_by_a_timeout(self, monkeypatch):
        """
        A wedged cupsd must not block the UI forever with the key buffer
        still resident and no way out but pulling the power.
        """
        seen = {}

        class Result:
            returncode = 0
            stdout = b""
            stderr = b""

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return Result()

        monkeypatch.setattr(printer.subprocess, "run", fake_run)
        printer.Cups()._run([printer.LPSTAT, "-p"])
        assert seen.get("timeout") == printer.Cups.TIMEOUT


class TestAdvisoryQueriesNeverEscape:
    """
    Queries are advisory. Letting a wedged cupsd's timeout out of one turns
    a slow printer into a crash loop with no panel message -- and the panel
    is the only place an operator can be told anything.
    """

    def test_a_wedged_cupsd_reads_as_nothing_to_report(self):
        cups = exploding_cups()
        assert cups.devices() == []
        assert cups._match_ppd(printer.Device("usb://HP/LaserJet?serial=1", "")) is None

    def test_a_failed_purge_never_propagates(self):
        exploding_cups().purge()          # must not raise


class TestOnlyLocallyAttachedPrintersAreAdopted:
    """
    The unit's whole point is being offline. Silently binding to a printer
    across the LAN would put key material on a network, and disabling the
    radios does not cover wired or USB ethernet.
    """

    def _cups(self, listing):
        class Result:
            returncode = 0
            stdout = listing.encode()
            stderr = b""

        return printer.Cups(run=lambda argv, stdin=None: Result())

    def test_a_lan_ipp_printer_is_not_adopted(self):
        assert self._cups("network ipp://192.168.1.50:631/ipp/print\n").devices() == []
        assert self._cups("network ipps://laser.example:631/ipp/print\n").devices() == []

    def test_the_local_ipp_usb_endpoint_still_is(self):
        for uri in ("ipp://localhost:60000/ipp/print",
                    "ipp://127.0.0.1:60000/ipp/print",
                    "ippusb://HP/LaserJet"):
            assert [d.uri for d in self._cups(f"network {uri}\n").devices()] == [uri]

    def test_a_dnssd_endpoint_on_its_own_is_the_network(self):
        # dnssd:// is how ipp-usb's local endpoint appears AND how a printer
        # across the LAN appears. With nothing plugged into USB, it is the
        # LAN, and adopting it would send a pad off the device.
        assert self._cups("network dnssd://Office%20Laser._ipp._tcp.local/\n") \
            .devices() == []

    def test_a_dnssd_endpoint_is_ipp_usb_only_when_it_names_the_usb_device(self):
        # This test as first written asserted that ANY usb:// device makes
        # ANY dnssd entry trustworthy, which is the LAN-binding bug itself:
        # "Office Laser" is not the plugged-in HP, and driverless endpoints
        # sort first, so that is what the queue would have bound.
        uris = [d.uri for d in self._cups(
            "network dnssd://Office%20Laser._ipp._tcp.local/\n"
            "direct usb://HP/LaserJet?serial=1\n").devices()]
        assert uris == ["usb://HP/LaserJet?serial=1"], uris

    def test_a_dnssd_endpoint_naming_the_usb_device_is_preferred(self):
        uris = [d.uri for d in self._cups(
            "network dnssd://HP%20LaserJet%20%5B0011%5D._ipp._tcp.local/\n"
            "direct usb://HP/LaserJet?serial=1\n").devices()]
        assert len(uris) == 2
        assert uris[0].startswith("dnssd://"), "driverless endpoints go first"


class TestEveryDeviceIsTried:
    def test_a_failed_driverless_setup_falls_through_to_the_usb_entry(self):
        """
        Driverless endpoints sort first because they need no driver, but when
        one fails to set up, the usb:// entry for the same printer is usually
        right behind it. Stopping at devices()[0] turns the preferred path
        into a single point of failure.
        """
        class TwoWays(FakeCups):
            def devices(self):
                return [printer.Device("ipp://localhost:60000/ipp/print", ""),
                        printer.Device("usb://HP/LaserJet?serial=1", "HP LaserJet")]

            def ensure_queue(self, device, name="OTP"):
                self.tried.append(device.uri)
                if device.uri.startswith("ipp"):
                    raise printer.PrinterError("no driver for HP LaserJet")
                return name

        cups = TwoWays()
        app = make_app([Press.QUIT], cups=cups)
        app.run()

        assert len(cups.tried) == 2, "the usb:// entry was never tried"
        assert len(app.stack) == 1, "a printer that works must not raise an error"


# --- the panel ----------------------------------------------------------


class TestPanelMessagesAreWholeWords:
    """
    A 21-column panel turns an over-long string into misinformation. Fixed
    slicing stops a driver error mid-word with no ellipsis, and it reads as
    if that were the whole message.
    """

    MESSAGE = "no driver for a printer with a long name"

    def test_rows_break_on_word_boundaries(self):
        rows = ui.wrap(self.MESSAGE)
        assert rows
        words = self.MESSAGE.split()
        for row in rows:
            assert len(row) <= 21
            for word in row.split():
                assert word in words, f"{word!r} is half a word, not a word"

    def test_a_dropped_tail_is_marked_rather_than_silently_lost(self):
        rows = ui.wrap("GENERATE FAILED: unable to open device file for printing",
                       lines=2)
        assert len(rows) == 2
        assert all(len(row) <= 21 for row in rows)
        assert rows[-1].endswith(">"), "a cut message must say it was cut"

    def test_an_over_long_word_is_cut_and_marked(self):
        assert ui.wrap("X" * 40) == ["X" * 20 + ">"]


class TestHomeUnwindsTheWholeStack:
    def test_home_returns_to_the_root_from_any_depth(self, monkeypatch):
        """
        HOME exists because a screen can only pop itself, and the screens
        underneath a finished job may not be reachable by any button
        sequence. Popping one frame drops the operator back into the middle
        of a codeword flow with the job already done.
        """
        class GoesHome(ui.Screen):
            def frame(self, app):
                return Frame(title="DEEP", lines=["DEEP"])

            def press(self, app, press):
                return ui.HOME

        def deep_menu():
            menu = ui.Menu([("DIVE", lambda app: ui.Menu(
                [("DEEPER", lambda a: GoesHome())]))])
            menu.title = "OTP PRINT UNIT"
            return menu

        monkeypatch.setattr(ui, "main_menu", deep_menu)
        app = make_app([Press.OK, Press.OK, Press.OK, Press.QUIT])
        app.run()

        assert len(app.stack) == 1, "HOME must unwind every frame, not one"
        assert isinstance(app.stack[0], ui.Menu)


class TestSettingsTheUnitCannotUseAreNeverCommitted:
    def test_an_out_of_range_value_is_reported_and_discarded(self):
        # The panel's own choosers only offer usable values, but _apply is
        # the gate in front of app.settings and the config file is not the
        # only way in. Committing a value validate() rejects is what puts
        # the unit into a state its own loader would have refused.
        app = make_app([])
        before = app.settings
        screen = ui._apply(app, font_size=99)

        assert app.settings == before, "an invalid setting must not be committed"
        assert isinstance(screen, ui.Message)
        assert "INVALID" in screen.title
        assert app.settings_saved is True, "nothing changed, so nothing is unsaved"

    def test_a_usable_value_is_committed_and_marked_unsaved(self):
        app = make_app([])
        assert ui._apply(app, pages=500) is None
        assert app.settings.pages == 500
        assert app.settings_saved is False


class TestUnsavedSettingsAreVisible:
    def test_the_panel_says_when_a_change_is_not_on_the_card(self):
        """
        The unit tells the operator to power-cycle after every pad job, and
        an unsaved TRAINING setting reverts to LIVE on the next boot -- so
        the next "training" batch prints unwatermarked and is
        indistinguishable from live key material.
        """
        app = make_app([])
        menu = ui.settings_menu()
        assert "UNSAVED" not in menu.frame(app).title

        ui._apply(app, pages=500)
        frame = menu.frame(app)

        assert "UNSAVED" in frame.title, "an unsaved change must be visible"
        assert frame.overflowing() == [], "and must still fit the panel"
        assert any(label.endswith("*") for label in menu.labels(app))


class TestAWipedJobIsFinished:
    def test_a_finished_job_cannot_spool_another_copy(self):
        """
        finish() drops the buffer as well as zeroing it. Keeping the
        reference leaves a job that still answers print_next_copy() -- and
        what it would spool is a pad's worth of zeroes.
        """
        cups = FakeCups()
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "RUSTED-BADGER",
                            config.Settings(pages=1, paper="A6"))
        job = jobs.PadPairJob(spec, cups)
        job.generate(progress=lambda d, t: None)
        job.print_next_copy()
        job.finish()

        assert job._buffer is None
        with pytest.raises(RuntimeError):
            job.print_next_copy()
        assert len(cups.submitted) == 1, "a wiped job must never spool zeroes"


class TestBankedPressesAreAlwaysDrained:
    def test_a_short_job_still_empties_the_press_queue(self):
        """
        Generation blocks the event loop, so presses made while it ran
        arrive afterwards aimed at a screen that is no longer there. One
        drain has to empty the queue however few pages the job had --
        draining one press per page leaves the rest to replay.
        """
        cups = FakeCups()
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "X-Y",
                            config.Settings(pages=1, paper="A6"))
        screen = ui.RunJob(spec)
        app = make_app([Press.UP, Press.DOWN, Press.UP, Press.DOWN, Press.UP],
                       cups=cups)

        screen.press(app, Press.OK)

        assert app.buttons._script == [], "banked presses must all be drained"
        assert screen.stage == "printing"
        assert len(cups.submitted) == 1, "copy B must wait for the swap prompt"


class TestTheJournalNeverSeesAJob:
    def test_generating_a_pad_prints_nothing_at_all(self, capsys):
        """
        The generator's own default progress reporter prints the codeword to
        stdout, which on the unit is the systemd journal -- the one place a
        codeword must never be written. jobs._silent exists only to stop
        that default applying, so nothing here may print.
        """
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "RUSTED-BADGER",
                            config.Settings(pages=2, paper="A6"))
        jobs.generate(spec)
        captured = capsys.readouterr()

        assert captured.out == "", captured.out
        assert "RUSTED-BADGER" not in captured.out + captured.err


# --- randomness ---------------------------------------------------------


class TestChoicesAreDrawnWithoutBias:
    """
    random_choice picks the codeword, and a codeword is how a pad is named
    and found. No distribution test can catch modulo bias over a 2**32 range
    -- the skew is one part in ten million -- so the rejection itself has to
    be observed.
    """

    def test_a_draw_in_the_biased_tail_is_redrawn(self, monkeypatch):
        seq = list("ABCDEFG")                    # n = 7
        limit = (2 ** 32 // 7) * 7               # the four values above this
        draws = [limit, limit + 3, 10]           # must not be folded back
        monkeypatch.setattr(g, "get_random_bytes",
                            lambda n: draws.pop(0).to_bytes(4, "big"))

        assert g.random_choice(seq) == seq[10 % 7]
        assert draws == [], "a value at or above the limit must be redrawn"

    def test_a_draw_below_the_limit_is_used_as_is(self, monkeypatch):
        monkeypatch.setattr(g, "get_random_bytes", lambda n: (10).to_bytes(4, "big"))
        assert g.random_choice(list("ABCDEFG")) == "D"

    def test_the_panels_codewords_come_from_the_same_csprng_as_the_key(
            self, monkeypatch):
        calls = []
        real = g.get_random_bytes
        monkeypatch.setattr(g, "get_random_bytes",
                            lambda n: calls.append(n) or real(n))
        vocabulary = cw.Vocabulary()

        word = vocabulary.random()
        modifier, noun = cw.split(word)

        assert calls, "the panel must not roll codewords with the random module"
        assert modifier in vocabulary.modifiers
        assert noun in vocabulary.all_nouns

    def test_codewords_are_distinct_even_when_the_space_is_nearly_exhausted(
            self, tmp_path):
        # Nine combinations exist. Asking for all nine can only succeed if a
        # repeat is rejected and redrawn -- and two pads wearing one codeword
        # is exactly what the naming convention exists to prevent.
        base = tiny_vocabulary(tmp_path)
        for _ in range(5):
            words = g.random_codewords(9, base)
            assert len(set(words)) == 9, words


# --- what the paper setting is supposed to produce -----------------------


class TestThePaperSettingReachesTheGeometry:
    """
    The imposition is baked into the PDF, so the paper setting is the only
    thing standing between the operator and a stack that cannot be cut. Both
    ways of getting it wrong are silent: every page still prints.
    """

    def test_the_default_is_four_pad_pages_per_a4_sheet(self):
        pages = pdf_pages(pad_pdf(pages=8))
        assert len(pages) == 2, "eight pad pages impose onto two A4 sheets"
        assert round(float(pages[0].mediabox.width)) == round(g.A4[0])

    def test_letter_gets_letter_geometry_not_a4(self):
        pages = pdf_pages(pad_pdf(pages=8, paper="LETTER"))
        assert len(pages) == 2
        assert round(float(pages[0].mediabox.width)) == round(g.LETTER[0])

    def test_a6_paper_is_one_pad_page_per_sheet(self):
        pages = pdf_pages(pad_pdf(pages=8, paper="A6"))
        assert len(pages) == 8
        assert round(float(pages[0].mediabox.width)) == round(g.A6[0])

    def test_a7_asks_the_driver_for_a6_whatever_the_paper_setting(self):
        # A7 emits landscape-A6 sheets with their own cut line. Sending
        # media=A4 would ask the driver to scale a sheet that is already the
        # right size, and the cut line would no longer be down the middle.
        for paper in ("A4", "LETTER", "A6"):
            assert config.Settings(a7=True, paper=paper).lp_options["media"] == "A6"

    def test_the_media_a_pad_job_asks_for_matches_its_paper_setting(self):
        for paper, media in (("A4", "A4"), ("LETTER", "Letter"), ("A6", "A6")):
            assert config.Settings(paper=paper).lp_options["media"] == media


class TestImposedSheetsCarryTheirCutMarks:
    def test_both_cuts_are_marked_at_both_ends(self):
        """
        Counting line segments is not enough: an imposed sheet draws eight,
        and four of those are the pad pages' own header rules. What makes a
        crop mark a crop mark is that it lies ON a cut line -- so look for
        segments collinear with the two cuts, and nowhere else.

        Deliberately not pinned to the exact tick length or inset. Those are
        printer-dependent (a laser cannot mark the outer few millimetres of
        a sheet), and a test that fixes them breaks on a legitimate change
        while saying nothing about whether the blade has anything to follow.
        """
        segments = line_segments(content_stream(pad_pdf(pages=4)))
        page_w, page_h = g.A4

        def on(value, target):
            return abs(value - target) <= 0.15

        vertical = [s for s in segments
                    if on(s[0], page_w / 2) and on(s[2], page_w / 2)]
        horizontal = [s for s in segments
                      if on(s[1], page_h / 2) and on(s[3], page_h / 2)]

        assert len(vertical) == 2, "the vertical cut needs a mark at each end"
        assert len(horizontal) == 2, "the horizontal cut needs a mark at each side"

        # Edge ticks, not full rules: a line across the sheet would run
        # through the key area. Each mark stays inside the outer quarter.
        for x1, y1, x2, y2 in vertical:
            assert max(y1, y2) < page_h / 4 or min(y1, y2) > 3 * page_h / 4, \
                "a crop mark must not run through the key area"
        for x1, y1, x2, y2 in horizontal:
            assert max(x1, x2) < page_w / 4 or min(x1, x2) > 3 * page_w / 4, \
                "a crop mark must not run through the key area"


class TestThePageNumberIsAFourDigitField:
    def test_a_five_digit_page_number_is_not_silently_wrapped(self):
        """
        Both ends drill against a fixed four-digit page number, so it must
        never wrap: page 10001 printing as 0001 puts two different pages
        under one label, and the pair would decrypt to nonsense with nothing
        on the page to say why.
        """
        sink = io.BytesIO()
        canvas = g.new_canvas(sink, g.A6)
        g.draw_pad_page(canvas, "RUSTED-BADGER", 10001, 35, 9,
                        groups_per_row=g.A6_GROUPS_PER_ROW,
                        chars_per_row=g.A6_CHARS_PER_ROW,
                        box_left=0, box_bottom=0,
                        box_width=g.SHEET_WIDTH, box_height=g.SHEET_HEIGHT)
        canvas.showPage()
        canvas.save()

        assert "10001" in page_text(sink.getvalue()), "page 10001 printed as 0001"


class TestTheDocumentGivesNothingAway:
    def test_no_field_of_the_info_dictionary_carries_anything(self):
        """
        The Info dictionary is not compressed, so a `strings` pass over a
        spooled job reads it directly. Pinning the title and the dates but
        leaving reportlab's defaults in Author, Subject and Creator leaves
        three fields saying something about where the pad was made.
        """
        pypdf = pytest.importorskip("pypdf")
        metadata = pypdf.PdfReader(io.BytesIO(pad_pdf(pages=1))).metadata

        assert metadata.get("/Title") == "OTP"
        for field in ("/Author", "/Subject", "/Creator"):
            assert metadata.get(field) == "", f"{field} = {metadata.get(field)!r}"


class TestTrainingMaterialIsMarkedWhereverItIsAskedFor:
    """
    otp.md requires practice material to be unmistakably marked. Both ways
    of asking for it -- the CLI flag and the unit's setting -- pass the flag
    down a chain, and a break anywhere in either chain prints an unmarked
    pad that is indistinguishable from live key material.
    """

    def test_the_units_training_setting_reaches_the_page(self):
        assert training_marks(pad_pdf(pages=1, paper="A6", training=True)) == 2
        assert training_marks(pad_pdf(pages=1, paper="A6")) == 0

    def test_the_confirm_screen_states_training_positively(self):
        app = make_app([])
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "RUSTED-BADGER",
                            config.Settings(pages=1, training=True))
        assert "TRAINING" in "\n".join(ui.RunJob(spec).frame(app).rendered())

    def test_the_cli_flag_reaches_the_page(self, tmp_path):
        result = cli(["--random-codewords", "1", "--pages", "1", "--training",
                      "--output", str(tmp_path)], cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        pdf = next(tmp_path.glob("*.pdf")).read_bytes()
        assert training_marks(pdf) == 2, "the watermark and the footer"


class TestEveryCopyAskedForIsPrinted:
    def test_the_tabula_menu_entry_prints_the_count_it_offers(self):
        # The menu offers two cards; printing one is a silent shortfall.
        spec = jobs.JobSpec(jobs.JobKind.TABULA, "", config.Settings(), count=3)
        assert len(pdf_pages(bytes(jobs.generate(spec)))) == 3

    def test_the_worksheet_menu_entry_prints_the_count_it_offers(self):
        spec = jobs.JobSpec(jobs.JobKind.WORKSHEETS, "", config.Settings(), count=4)
        assert len(pdf_pages(bytes(jobs.generate(spec)))) == 4


class TestTheWorksheetTeachesBothDirections:
    def _worksheet(self) -> bytes:
        sink = io.BytesIO()
        g.generate_worksheets_pdf(sink, 1)
        return sink.getvalue()

    def test_it_says_how_to_decrypt_not_only_how_to_encrypt(self):
        # Two of the manual's three exercises are decrypts, where the
        # ciphertext goes in the top row and the plaintext comes out of the
        # shaded one. A student following the M/K/C labels alone walks
        # straight into the wrong arithmetic.
        text = page_text(self._worksheet())
        assert "ENCRYPT M+K=C" in text
        assert "DECRYPT" in text and "C IN THE TOP ROW" in text

    def test_the_result_row_is_shaded_on_every_block(self):
        data = self._worksheet()
        blocks = [row for row in glyph_rows(data) if row == "C"]
        assert blocks, "the worksheet must have C rows at all"
        # One wide grey fill per block. The narrow rectangles are the
        # letter cells, which are about 13pt across.
        shaded = [rect for rect in rectangles(content_stream(data))
                  if rect[2] > 100]
        assert len(shaded) == len(blocks), "the answer row must be findable"


# --- the guards in front of a useless pad --------------------------------


class TestAHandEditedConfigCannotPrintAUselessPad:
    """
    The config file's own header invites the operator to edit it by hand, so
    every bound the panel enforces has to be enforced again on load. These
    two are the ones whose failure is invisible: the pad prints, and it is
    the printing that is wrong.
    """

    def test_a_font_that_only_overflows_sideways_is_still_rejected(self):
        settings = config.Settings(font_size=14)
        # 14pt fits the page height, so the chars-per-page check is happy.
        assert settings.chars_per_page <= g.calc_max_chars(
            settings.font_size, settings.a7, settings._box_height()), \
            "this size must be caught by the width check, not the height one"
        # It is the width that fails: the inter-group gap goes negative and
        # the five-letter groups print superimposed, inside the content box,
        # so nothing overflows visibly and both copies are equally unreadable.
        assert settings.validate() == ["font size too large for the page width"]

    def test_an_auth_group_that_would_overprint_the_page_number_is_rejected(self):
        # AUTH and the page number are the two fields deliberately kept at
        # full size, because they are read character by character.
        assert config.Settings(auth_size=36).validate() == \
            ["auth group too wide for the header"]
        assert config.Settings(auth_size=8).validate() == []

    def test_load_falls_back_rather_than_accepting_either(self, tmp_path):
        path = tmp_path / "otp-unit.conf"
        path.write_text("font_size = 14\nauth_size = 36\npages = 50\n")
        loaded = config.load(str(path))
        assert loaded.validate() == [], "load() must never return a bad config"
        assert loaded.pages == 50, "the sane field should survive"


class TestTheCliRejectsWhatItCannotDraw:
    def test_fontsize_must_be_a_positive_number(self, tmp_path):
        # nan is the interesting one: it compares False against every bound,
        # so a bare `<= 0` check lets it through to calc_max_chars, which
        # raises int(nan) with a traceback instead of a message.
        for bad in ("0", "-3", "nan"):
            result = cli(["--random-codewords", "1", "--pages", "1",
                          "--fontsize", bad, "--output", str(tmp_path)],
                         cwd=tmp_path)
            assert result.returncode != 0, bad
            assert b"--fontsize must be a positive number" in result.stderr, bad

    def test_a_usable_fontsize_is_still_accepted(self, tmp_path):
        result = cli(["--random-codewords", "1", "--pages", "1",
                      "--fontsize", "8", "--output", str(tmp_path)], cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_tabula_must_be_zero_or_more(self, tmp_path):
        result = cli(["--tabula", "-1", "--output", str(tmp_path)], cwd=tmp_path)
        assert result.returncode != 0
        assert b"--tabula must be 0 or more" in result.stderr


# --- the hardware layer -------------------------------------------------


class TestAStalePressTimeCannotReFire:
    def test_a_second_release_does_not_replay_the_last_press(self, monkeypatch):
        """
        The press time is cleared as it is read. Leaving it set means a
        stray release -- bounce on the way up, or a press edge lost to the
        debounce window -- is timed against the previous press and emits a
        second event. While a job is printing OK and BACK mean opposite
        things, so a phantom BACK purges the spool.
        """
        import queue
        from otpunit.hw import buttons as buttons_mod

        unit = buttons_mod.GpioButtons.__new__(buttons_mod.GpioButtons)
        unit._events = queue.Queue()
        unit._buttons = []
        unit._pressed_at = None

        now = [1000.0]
        monkeypatch.setattr(buttons_mod.time, "monotonic", lambda: now[0])

        unit._on_press()
        now[0] += 0.05
        unit._on_release()
        assert unit.wait(timeout=0) is Press.OK

        now[0] += 2.0
        unit._on_release()
        assert unit.wait(timeout=0) is None, "a stale press time re-fired"
