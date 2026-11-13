' The Watch page's lineup (static/tunable-channels.js) for the Roku: the
' TV Thing numbers from the source catalogs, with the station at each house
' number, its slide channels and the internet streams. Unnumbered channels
' follow by name; tuning still uses source ids. Pure functions, shared by
' LineupTask and the scene.
'
' An entry: {id, kind ("vizelvision" | "slides" | "internet" | "digital" |
'            "roku"), name (the lineup's name: a call sign), key (its number,
'            or a short source name), detail (what is on, or a note), label
'            (where it comes from: "Antenna 44.1", "YouTube TV"), section,
'            tag, enabled, progress (-1 or 0-100), events ([{start, finish,
'            title, description, live, captions}], Unix seconds), sourceId,
'            channelId, date, streamPath, sortKey, number}. streamPath is set
'            for the channels that play a TV Thing relay or video
'            (VizelVision, the Sports Guide, internet streams): nothing is
'            tuned and no lock is involved.

function LineupBuild(channels as Object, sources as Object, feeds as Object, date as String, guide as Dynamic, nowSeconds as Integer, numbering = invalid as Dynamic) as Object
    rest = LineupStations(numbering, guide, nowSeconds)
    for each channel in channels
        number = LineupText(channel.number)
        channelId = LineupText(channel.id)
        entry = LineupEntry(LineupDigitalId(channelId), "digital", LineupText(channel.name), number, "Antenna " + number + " · RF " + LineupText(channel.rf_channel), "ATSC")
        entry.sourceId = "digital"
        entry.channelId = channelId
        entry.label = "Antenna " + number
        LineupNumbered(entry, channel)
        LineupListing(entry, guide, number, nowSeconds)
        rest.Push(entry)
    end for
    for each source in sources
        sourceId = LineupText(source.id)
        dated = LineupIsTrue(source.dated)
        items = feeds[sourceId]
        if items = invalid then items = []
        for each item in items
            itemId = LineupText(item.id)
            itemDate = ""
            key = LineupShortName(sourceId)
            note = LineupText(item.note)
            if dated
                itemDate = date
                clock = LineupGameClock(item)
                if clock <> "" then key = clock
                note = LineupText(item.status)
            end if
            parts = [LineupText(source.name)]
            ' The number takes the key column; keep the game clock in its detail.
            if dated then parts.Push(key)
            parts.Push(note)
            if LineupIsObject(item.route) then parts.Push(LineupText(item.route.label))
            entry = LineupEntry(LineupRokuId(sourceId, itemDate, itemId), "roku", LineupText(item.name), key, LineupJoin(parts, " · "), "ROKU")
            entry.enabled = not LineupIsFalse(source.available) and not LineupIsFalse(item.playable)
            entry.sourceId = sourceId
            entry.channelId = itemId
            entry.date = itemDate
            entry.label = LineupText(source.name)
            LineupNumbered(entry, item)
            ' YouTube TV's channels are guide rows by source and channel; a
            ' day's games are rows by their number.
            if not dated
                LineupListing(entry, guide, sourceId + "/" + itemId, nowSeconds)
            else if entry.number <> invalid
                LineupListing(entry, guide, "games/" + entry.key, nowSeconds)
            end if
            rest.Push(entry)
        end for
    end for
    for each entry in rest
        prefix = "2:"
        if entry.kind = "vizelvision" then prefix = "0:"
        if entry.number <> invalid then prefix = "1:" + LineupPadDigits(entry.key) + ":"
        entry.sortKey = prefix + LineupSortKey(entry.name, entry.id)
    end for
    rest.SortBy("sortKey", "")
    return rest
end function

function LineupEntry(id as String, kind as String, name as String, key as String, detail as String, tag as String) as Object
    return {id: id, kind: kind, name: name, key: key, detail: detail, label: "", section: "", tag: tag, enabled: true, progress: -1, events: [], sourceId: "", channelId: "", date: "", streamPath: "", sortKey: "", number: invalid}
end function

' VizelVision, through TV Thing's watch-only relay of the master's feed.
function LineupStation() as Object
    entry = LineupEntry("vizelvision", "vizelvision", "VizelVision", "VV", "The station’s live broadcast", "LIVE")
    entry.streamPath = "/stream/vizelvision/index.m3u8"
    entry.label = "House channel"
    return entry
end function

' Channel 0, VizelVision: where the app starts when nothing was watched yet
' (the lineup's house number; the same id the lineup gives it).
function LineupChannelZero() as Object
    entry = LineupStation()
    entry.id = "vizelvision:0"
    entry.key = "0"
    entry.number = 0
    return entry
end function

' The channels that play a stream TV Thing already makes or relays: the
' station at its house numbers, its slide channels and the internet streams.
function LineupStations(numbering as Dynamic, guide as Dynamic, nowSeconds as Integer) as Object
    stations = []
    if LineupIsObject(numbering) and type(numbering.house) = "roArray"
        for each view in numbering.house
            entry = LineupNumbered(LineupStation(), view)
            if entry.number <> invalid then entry.id = "vizelvision:" + entry.key
            stations.Push(entry)
        end for
    end if
    if stations.Count() = 0 then stations.Push(LineupStation())
    ' The Sports Guide on 60: TV Thing's video of the slides, made while watched.
    if LineupIsObject(numbering) and type(numbering.slides) = "roArray"
        for each view in numbering.slides
            if LineupIsObject(view) and LineupText(view.stream_path) <> ""
                channelId = LineupText(view.channel_id)
                entry = LineupEntry("slides:" + channelId, "slides", "Sports Guide", "SG", "Today’s games by sport, with their channels", "GUIDE")
                entry.channelId = channelId
                entry.streamPath = LineupText(view.stream_path)
                entry.label = "VizelVision"
                stations.Push(LineupNumbered(entry, view))
            end if
        end for
    end if
    ' Internet streams (internet_streams.json) play the server's relay of them.
    if LineupIsObject(numbering) and type(numbering.internet) = "roArray"
        for each view in numbering.internet
            if LineupIsObject(view) and LineupText(view.stream_path) <> ""
                channelId = LineupText(view.channel_id)
                note = LineupText(view.note)
                if note = "" then note = "Internet stream"
                entry = LineupEntry("internet:" + channelId, "internet", LineupText(view.name), "WEB", note, "WEB")
                entry.sourceId = "internet"
                entry.channelId = channelId
                entry.streamPath = LineupText(view.stream_path)
                entry.label = "Internet stream"
                LineupNumbered(entry, view)
                LineupListing(entry, guide, "internet/" + channelId, nowSeconds)
                stations.Push(entry)
            end if
        end for
    end if
    return stations
end function

' The channel's listings from api/v1/guide's row at key, when it has one:
' its events (finished ones dropped), and what is on now as its detail.
sub LineupListing(entry as Object, guide as Dynamic, key as String, nowSeconds as Integer)
    if not LineupIsObject(guide) then return
    row = guide[key]
    if not LineupIsObject(row) or type(row.events) <> "roArray" then return
    events = []
    for each e in row.events
        if LineupIsObject(e) and LineupIsNumber(e.start) and LineupIsNumber(e.duration)
            start = LineupSeconds(e.start)
            finish = start + LineupSeconds(e.duration)
            if finish > nowSeconds
                title = LineupText(e.title)
                if title = "" then title = "No title"
                description = LineupText(e.description)
                if description = "" then description = LineupText(e.episode_title)
                events.Push({start: start, finish: finish, title: title, description: description, live: LineupIsTrue(e.live), captions: LineupIsTrue(e.captions)})
            end if
        end if
    end for
    events.SortBy("start")
    entry.events = events
    if events.Count() > 0 and events[0].start <= nowSeconds
        entry.detail = events[0].title
        entry.progress = LineupProgress(events[0], nowSeconds)
    end if
end sub

function LineupNumbered(entry as Object, view as Dynamic) as Object
    entry.number = invalid
    if not LineupIsObject(view) then return entry
    value = view.tv_number
    kind = type(value)
    if kind = "Integer" or kind = "roInt" or kind = "roInteger" or kind = "LongInteger" or kind = "roLongInteger"
        entry.number = value
        entry.key = LineupText(value)
    end if
    name = LineupText(view.tv_name)
    if name <> "" then entry.name = name
    entry.section = LineupText(view.tv_section)
    return entry
end function

function LineupDigitalId(channelId as String) as String
    return "digital:" + channelId
end function

function LineupRokuId(sourceId as String, date as String, channelId as String) as String
    if date <> "" then return "roku:" + sourceId + ":" + date + ":" + channelId
    return "roku:" + sourceId + ":" + channelId
end function

' "digital", "roku" or "analog". A pool tuner's id ("viviserv2:digital") is
' always an antenna tuner.
function LineupLaneKind(lane as String) as String
    if Instr(1, lane, ":") > 0 then return "digital"
    return lane
end function

' The entry id a lane's selection (or a navigation's target) shows, or "".
function LineupSelectionId(lane as String, selection as Dynamic) as String
    if not LineupIsObject(selection) then return ""
    if LineupLaneKind(lane) = "digital"
        channelId = LineupText(selection.id)
        if channelId = "" then return ""
        return LineupDigitalId(channelId)
    end if
    if LineupIsTrue(selection.manual) then return ""
    sourceId = LineupText(selection.source_id)
    channelId = LineupText(selection.channel_id)
    if sourceId = "" or channelId = "" then return ""
    return LineupRokuId(sourceId, LineupText(selection.date), channelId)
end function

' The Watch page's short source names (static/index.html ROKU_SHORT).
function LineupShortName(sourceId as String) as String
    names = {"youtube-tv": "YTTV", "mlb": "MLB", "thursday-night-football": "TNF", "sunday-ticket": "NFL", "nhl": "NHL", "nwsl": "NWSL"}
    if names.DoesExist(sourceId) then return names[sourceId]
    return "RK"
end function

' Case-insensitive, with digit runs compared by value, like localeCompare's
' numeric option; the id breaks ties.
function LineupSortKey(name as String, id as String) as String
    key = ""
    digits = ""
    text = LCase(name)
    for i = 1 to Len(text)
        ch = Mid(text, i, 1)
        code = Asc(ch)
        if code >= 48 and code <= 57
            digits = digits + ch
        else
            if digits <> "" then key = key + LineupPadDigits(digits)
            digits = ""
            key = key + ch
        end if
    end for
    if digits <> "" then key = key + LineupPadDigits(digits)
    return key + Chr(1) + LCase(id)
end function

function LineupPadDigits(digits as String) as String
    if Len(digits) >= 10 then return digits
    return Right("0000000000" + digits, 10)
end function

function LineupGameClock(item as Object) as String
    state = LineupText(item.state)
    if state = "live" or state = "in" then return "LIVE"
    if state = "final" or state = "post" then return "FINAL"
    return LineupLocalTime(LineupText(item.start_time))
end function

function LineupProgress(event as Object, nowSeconds as Integer) as Integer
    duration = event.finish - event.start
    if duration <= 0 then return -1
    percent = Int((nowSeconds - event.start) * 100 / duration)
    if percent < 0 then return 0
    if percent > 100 then return 100
    return percent
end function

' "4:25 PM" for an ISO time, in the Roku's time zone; "" if unreadable.
function LineupLocalTime(iso as String) as String
    seconds = LineupIsoSeconds(iso)
    if seconds <= 0 then return ""
    return LineupClockAt(seconds)
end function

' "8:00 PM" for Unix seconds, in the Roku's time zone.
function LineupClockAt(seconds as Dynamic) as String
    dt = CreateObject("roDateTime")
    dt.FromSeconds(seconds)
    dt.ToLocalTime()
    return LineupClockText(dt)
end function

' Seconds since 1970 for "2026-10-18T21:08:00Z", "2026-10-18T17:00Z" (no
' seconds) or a time with a "+hh:mm" / "-hh:mm" offset; 0 if unreadable.
function LineupIsoSeconds(iso as String) as Integer
    if Len(iso) < 16 or Mid(iso, 11, 1) <> "T" then return 0
    clock = Mid(iso, 12)
    zone = Len(clock) + 1
    for i = 1 to Len(clock)
        ch = Mid(clock, i, 1)
        if ch = "Z" or ch = "z" or ch = "+" or ch = "-"
            zone = i
            exit for
        end if
    end for
    offset = 0
    sign = Mid(clock, zone, 1)
    if sign = "+" or sign = "-"
        spec = Mid(clock, zone + 1)
        hours = 0
        minutes = 0
        if Instr(1, spec, ":") > 0
            parts = spec.Split(":")
            hours = Int(Val(parts[0]))
            minutes = Int(Val(parts[1]))
        else if Len(spec) = 4
            hours = Int(Val(Left(spec, 2)))
            minutes = Int(Val(Right(spec, 2)))
        else
            hours = Int(Val(spec))
        end if
        offset = (hours * 60 + minutes) * 60
        if sign = "-" then offset = -offset
    end if
    fields = Left(clock, zone - 1).Split(":")
    if fields.Count() < 2 then return 0
    second = 0
    if fields.Count() >= 3 then second = Int(Val(fields[2]))
    text = Left(iso, 10) + "T" + LineupTwo(Int(Val(fields[0]))) + ":" + LineupTwo(Int(Val(fields[1]))) + ":" + LineupTwo(second) + "Z"
    dt = CreateObject("roDateTime")
    dt.FromISO8601String(text)
    ' A string it cannot read is not an error, so check what it set.
    if dt.GetYear() <> Int(Val(Left(iso, 4))) or dt.GetMonth() <> Int(Val(Mid(iso, 6, 2))) or dt.GetDayOfMonth() <> Int(Val(Mid(iso, 9, 2))) then return 0
    return dt.AsSeconds() - offset
end function

function LineupClockText(dt as Object) as String
    hours = dt.GetHours()
    suffix = "AM"
    if hours >= 12 then suffix = "PM"
    hours = hours MOD 12
    if hours = 0 then hours = 12
    return hours.ToStr() + ":" + LineupTwo(dt.GetMinutes()) + " " + suffix
end function

' Today in the Roku's time zone, as the dated sources want it.
function LineupLocalDate() as String
    dt = CreateObject("roDateTime")
    dt.ToLocalTime()
    return dt.GetYear().ToStr() + "-" + LineupTwo(dt.GetMonth()) + "-" + LineupTwo(dt.GetDayOfMonth())
end function

function LineupTwo(value as Integer) as String
    return Right("0" + value.ToStr(), 2)
end function

function LineupJoin(parts as Object, separator as String) as String
    text = ""
    for each part in parts
        if part <> ""
            if text <> "" then text = text + separator
            text = text + part
        end if
    end for
    return text
end function

' JSON values as text: numbers and booleans spelled out, null as "".
function LineupText(value as Dynamic) as String
    kind = type(value)
    if kind = "String" or kind = "roString" then return value
    if LineupIsNumber(value) or kind = "Boolean" or kind = "roBoolean"
        return value.ToStr()
    end if
    return ""
end function

'Unix seconds as a whole number. Int() takes a 32-bit Float, which would
' round a time to the nearest minute or so, so whole numbers stay as they are.
function LineupSeconds(value as Dynamic) as Dynamic
    kind = type(value)
    if kind = "Integer" or kind = "roInt" or kind = "roInteger" or kind = "LongInteger" or kind = "roLongInteger" then return value
    return Int(value)
end function

function LineupIsNumber(value as Dynamic) as Boolean
    kind = type(value)
    return kind = "Integer" or kind = "roInt" or kind = "roInteger" or kind = "LongInteger" or kind = "roLongInteger" or kind = "Float" or kind = "roFloat" or kind = "Double" or kind = "roDouble"
end function

function LineupIsTrue(value as Dynamic) as Boolean
    kind = type(value)
    if kind = "Boolean" or kind = "roBoolean" then return value
    return false
end function

function LineupIsFalse(value as Dynamic) as Boolean
    kind = type(value)
    if kind = "Boolean" or kind = "roBoolean" then return not value
    return false
end function

' A JSON object (associative array).
function LineupIsObject(value as Dynamic) as Boolean
    return type(value) = "roAssociativeArray"
end function
