' Run by test_roku_app.py with components/Guide.brs appended: the grid's
' rows, cells and Left / Right, as static/guide.html lays them out.
sub Main()
    e = 1792969200            ' a half hour: the grid's left edge
    horizon = e + 6 * 3600
    wzdc = {kind: "digital", name: "WZDC", detail: "", events: [
        {start: e - 120, finish: e + 7200, title: "Juego de Villanos", description: "", live: false, captions: false},
        {start: e + 7200, finish: e + 9000, title: "Pica y se extiende", description: "", live: false, captions: true}]}
    rows = GuideSegments(wzdc, e, horizon)
    Check(rows.Count() = 3, "two programmes and the gap after them")
    Check(rows[0].start = e - 120 and rows[0].listed, "a programme already on keeps its real start")
    Check(rows[1].title = "Pica y se extiende" and rows[1].captions, "the next programme follows")
    Check(rows[2].gap and rows[2].title = "No information" and rows[2].finish = horizon, "unlisted time is a gap to the horizon")
    Check(GuideSegmentAt(rows, e + 7300) = 1 and GuideSegmentAt(rows, e) = 0, "the programme on at a time")

    cells = GuideCells(rows, e, 7200, 0, 1058)
    Check(cells.Count() = 1, "only what falls in the window")
    Check(cells[0].x = 0 and cells[0].w = 1058 and cells[0].before and not cells[0].after and cells[0].focused, "a two-hour programme fills the window, marked as begun")
    later = GuideCells(rows, e + 5400, 7200, 1, 1058)
    Check(later.Count() = 3 and later[1].x = 264 and later[1].w = 265 and later[1].focused, "cells sit at their time across the track")
    Check(later[2].gap and later[2].after, "a cell running past the window is marked")

    ' Right: the next programme, the window following it in half hours.
    move = GuideNeighbour(rows, 0, 1, e, 7200, e, horizon - 7200)
    Check(move <> invalid and move.index = 1 and move.windowStart = e + 7200 and move.time = e + 7200, "Right moves on to the next programme")
    Check(GuideNeighbour(rows, 0, -1, e, 7200, e, horizon - 7200) = invalid, "Left stops at the present")
    back = GuideNeighbour(rows, 1, -1, e + 7200, 7200, e, horizon - 7200)
    Check(back.index = 0 and back.windowStart = e and back.time = e, "Left goes back, never before now")
    last = GuideNeighbour(rows, 1, 1, e + 7200, 7200, e, horizon - 7200)
    Check(last.index = 2 and last.windowStart = e + 7200, "a programme inside the window keeps it")
    Check(GuideNeighbour(rows, 2, 1, e + 7200, 7200, e, horizon - 7200) = invalid, "Right stops at the horizon")

    ' Channels without listings.
    antenna = GuideSegments({kind: "digital", name: "WFDC-DT", detail: "", events: []}, e, horizon)
    Check(antenna.Count() = 1 and antenna[0].gap and antenna[0].title = "No information", "an antenna channel without listings says so")
    pentulive = GuideSegments({kind: "internet", name: "Pentulive", detail: "Yle's puppy cam", events: []}, e, horizon)
    Check(pentulive.Count() = 1 and not pentulive[0].gap and not pentulive[0].listed, "an always-on stream is one programme")
    Check(pentulive[0].title = "Pentulive" and pentulive[0].description = "Yle's puppy cam", "named for what it plays")

    ' Overlapping listings never overlap on screen.
    overlap = GuideSegments({kind: "roku", name: "X", detail: "", events: [
        {start: e, finish: e + 3600, title: "A", description: "", live: false, captions: false},
        {start: e + 3000, finish: e + 5000, title: "B", description: "", live: true, captions: false}]}, e, horizon)
    Check(overlap[1].start = e + 3600 and overlap[1].live, "a programme overlapping the one before starts after it")

    ' A programme that ended before now is gone from the listings: a blank
    ' stretch, never "No information", and Left stops at it.
    cab = {kind: "internet", name: "Cab Ride", detail: "", events: [
        {start: e + 1020, finish: e + 8400, title: "Bergen Line", description: "", live: false, captions: false}]}
    gone = GuideSegments(cab, e, horizon, e + 1440)
    Check(gone[0].gap and gone[0].past and gone[0].title = "" and gone[0].finish = e + 1020, "time already gone is blank")
    Check(not gone[2].past and gone[2].title = "No information", "a gap still to come says so")
    Check(GuideSegmentAt(gone, e + 1440) = 1, "the highlight starts on what is on")
    Check(GuideNeighbour(gone, 1, -1, e, 7200, e, horizon - 7200) = invalid, "Left stops before time already gone")

    ' The banner's NOW and LATER.
    Check(GuideNow(wzdc, e + 60).title = "Juego de Villanos", "now")
    Check(not GuideNow(wzdc, e + 9100).listed, "after the listings: nothing listed")
    Check(GuideLater(wzdc, e + 60, 3).Count() = 1 and GuideLater(wzdc, e + 60, 3)[0].title = "Pica y se extiende", "later")
    Check(GuideHalfHour(e + 1799) = e and GuideHalfHour(e + 1800) = e + 1800, "half hours")
    print "GUIDE SCENARIOS PASSED"
end sub

sub Check(ok as Boolean, message as String)
    if not ok then print "FAIL: " + message
end sub
