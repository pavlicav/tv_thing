' The cable-box guide's grid (static/guide.html) as pure functions, shared
' by the scene and the tests: a channel's row of programmes, where each
' falls in the visible window, and where Left / Right go.
'
' A segment: {start, finish, title, description, listed, gap, past, live,
' captions}. A row runs from the half hour now began (the grid never shows
' earlier) to its horizon, with no holes: unlisted time is a gap ("No
' information"), and a channel with no listings at all (an internet stream,
' VizelVision, a game) is one long segment named for what it plays. TV
' Thing drops programmes that have finished, so time already gone before the
' first listing is a blank gap (past), not "No information".

function GuideSegments(entry as Object, fromTime as Integer, toTime as Integer, nowTime = 0 as Integer) as Object
    segments = []
    events = entry.events
    if type(events) <> "roArray" or events.Count() = 0
        if entry.kind = "digital"
            segments.Push(GuideGap(fromTime, toTime, nowTime))
        else
            segments.Push({start: fromTime, finish: toTime, title: entry.name, description: entry.detail, listed: false, gap: false, past: false, live: false, captions: false})
        end if
        return segments
    end if
    t = fromTime
    for each e in events
        if e.finish > t and e.start < toTime
            start = e.start
            ' The first programme keeps its real start (it began before the
            ' grid); a later one that overlaps its neighbour starts after it.
            if start < t and segments.Count() > 0 then start = t
            if start > t then segments.Push(GuideGap(t, start, nowTime))
            segments.Push({start: start, finish: e.finish, title: e.title, description: e.description, listed: true, gap: false, past: false, live: e.live, captions: e.captions})
            t = e.finish
        end if
    end for
    if t < toTime then segments.Push(GuideGap(t, toTime, nowTime))
    return segments
end function

function GuideGap(start as Integer, finish as Integer, nowTime = 0 as Integer) as Object
    past = finish <= nowTime
    title = "No information"
    if past then title = ""
    return {start: start, finish: finish, title: title, description: "", listed: false, gap: true, past: past, live: false, captions: false}
end function

' The segment on at time (or the last one).
function GuideSegmentAt(segments as Object, time as Integer) as Integer
    for i = 0 to segments.Count() - 1
        if segments[i].finish > time then return i
    end for
    return segments.Count() - 1
end function

' The row's cells inside the window [windowStart, windowStart + span), as
' x and width in pixels across width; before / after mark a programme that
' runs off either edge.
function GuideCells(segments as Object, windowStart as Integer, span as Integer, focus as Integer, width as Integer) as Object
    cells = []
    windowEnd = windowStart + span
    for i = 0 to segments.Count() - 1
        s = segments[i]
        if s.finish > windowStart and s.start < windowEnd
            a = s.start
            if a < windowStart then a = windowStart
            b = s.finish
            if b > windowEnd then b = windowEnd
            x = Int((a - windowStart) * width / span)
            edge = Int((b - windowStart) * width / span)
            cells.Push({x: x, w: edge - x, title: s.title, gap: s.gap, focused: i = focus, before: s.start < windowStart, after: s.finish > windowEnd, captions: s.captions})
        end if
    end for
    return cells
end function

' Left (delta -1) or Right (+1) from segment focus: the neighbour, the
' window start that shows it (half-hour steps, never before earliest or
' after latest) and the time the focus now follows down the rows; invalid
' at either end of the row, and before a blank stretch already gone.
function GuideNeighbour(segments as Object, focus as Integer, delta as Integer, windowStart as Integer, span as Integer, earliest as Integer, latest as Integer) as Dynamic
    index = focus + delta
    if index < 0 or index >= segments.Count() then return invalid
    s = segments[index]
    if s.past then return invalid
    start = windowStart
    if s.start < windowStart or s.start >= windowStart + span then start = GuideHalfHour(s.start)
    if start < earliest then start = earliest
    if start > latest then start = latest
    time = s.start
    if time < start then time = start
    return {index: index, windowStart: start, time: time}
end function

function GuideHalfHour(seconds as Integer) as Integer
    return seconds - (seconds MOD 1800)
end function

' The programme on a channel at time, for the banner: {title, start,
' finish, description, listed}.
function GuideNow(entry as Object, time as Integer) as Object
    segments = GuideSegments(entry, time, time + 1)
    return segments[0]
end function

' The listed programmes after the one on at time, up to count.
function GuideLater(entry as Object, time as Integer, count as Integer) as Object
    later = []
    if type(entry.events) <> "roArray" then return later
    for each e in entry.events
        if e.start > time and later.Count() < count then later.Push(e)
    end for
    return later
end function
