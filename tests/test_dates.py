# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
import datetime as dt

from aha_local.dates import note

TUESDAY = dt.date(2026, 11, 24)


def test_the_weekday_isnt_joined_with_a_number_from_elsewhere():
    """Seen: "Tuesday, 2pm, for 2 people" on Tuesday 24 Nov booked as "Tuesday 2 December" (a Wednesday)."""
    line = note("Book a table on flatiron KingsX, Tuesday, 2pm, for 2 people.", TUESDAY)
    assert "today is Tuesday 24 November 2026; the next one is Tuesday 1 December 2026" in line
    assert "2 December" not in line


def test_days_and_dates_are_worked_out():
    assert "“Friday” = Friday 27 November 2026 (the one after: Friday 4 December 2026)" in note("Friday at 8", TUESDAY)
    assert "“tomorrow” = Wednesday 25 November 2026" in note("tomorrow 9am", TUESDAY)
    assert "“the 2nd” = Wednesday 2 December 2026" in note("on the 2nd at 1pm", TUESDAY)
    assert "“2 Dec” = Wednesday 2 December 2026" in note("2 Dec at 2pm", TUESDAY)
    assert "Thursday 26 November 2026" in note("reservar quinta-feira 20h", TUESDAY)
    assert "Saturday 28 November 2026" in note("Sat 7pm", TUESDAY)


def test_ordinary_words_and_numbers_arent_dates():
    for text in ("I sat down", "book for 2 at 2pm", "2 out of 5 stars", "we may go", "the sun is out"):
        assert note(text, TUESDAY) == "", text
