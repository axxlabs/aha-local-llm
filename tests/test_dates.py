# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
import datetime as dt

from aha_local.dates import note

TUESDAY = dt.date(2026, 11, 24)


def test_the_weekday_isnt_joined_with_a_number_from_elsewhere():
    """Seen: "Tuesday, 2pm, for 2 people" on Tuesday 24 Nov booked as "Tuesday 2 December" (a Wednesday)."""
    line = note("Book a table on flatiron KingsX, Tuesday, 2pm, for 2 people.", TUESDAY)
    assert "“Tuesday” = 24/11/2026 (today), or 01/12/2026 (the next one)" in line
    assert "02/12" not in line


def test_days_and_dates_are_worked_out_as_full_dates():
    assert "“Friday” = 27/11/2026 (the one after: 04/12/2026)" in note("Friday at 8", TUESDAY)
    assert "“next Friday” = 27/11/2026, or 04/12/2026 if they mean the week after" in note("next Friday", TUESDAY)
    assert "“tomorrow” = 25/11/2026" in note("tomorrow 9am", TUESDAY)
    assert "“the 2nd” = 02/12/2026" in note("on the 2nd at 1pm", TUESDAY)
    assert "“2 Dec” = 02/12/2026" in note("2 Dec at 2pm", TUESDAY)
    assert "26/11/2026" in note("reservar quinta-feira 20h", TUESDAY)
    assert "28/11/2026" in note("Sat 7pm", TUESDAY)
    assert "“this weekend” = 28/11/2026 to 29/11/2026" in note("this weekend", TUESDAY)
    assert "“next week” = 30/11/2026 to 06/12/2026" in note("next week", TUESDAY)


def test_ordinary_words_and_numbers_arent_dates():
    for text in ("I sat down", "book for 2 at 2pm", "2 out of 5 stars", "we may go", "the sun is out"):
        assert note(text, TUESDAY) == "", text
