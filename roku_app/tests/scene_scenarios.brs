' Run by test_roku_app.py with the scene's actual handlers appended: locked
' tunes, relays (internet streams, the Sports Guide, VizelVision), why one
' will not play, falling back to channel 0, and the remote. SceneGraph,
' playback and HTTP are doubles.
sub Main()
    for each lane in ["digital", "roku"]
        for each statusCase in ["different", "failed", "missing"]
            Setup(lane)
            OnTune(7, {code: 423, data: {lock: {owner: "VizelVision", reason: "On air"}}})
            Check(m.pending.phase = "locked", "423 waits for status")
            Check(m.polls = 1, "423 refreshes status")
            if statusCase = "failed"
                OnStatus({code: 0})
            else if statusCase = "missing"
                OnStatus({code: 200, data: {lanes: {}}})
            else
                OnStatus({code: 200, data: {lanes: m.lanes}})
            end if
            Check(m.pending = invalid, "refused request is cleared")
            Check(m.watching.id = "previous", "previous channel is retained")
            Check(m.watchLane = "" and m.watchStream = "original", "playback attachment is retained")
            Check(m.bannerEntry.id = "previous", "banner returns to previous channel")
            Check(m.surfIndex = -1 and m.surfTimer.control = "stop", "surf preview is cleared")
            Check(Instr(1, m.top.dialog.message[0], "NHK World") > 0, "notice names requested channel")
            Check(Instr(1, m.top.dialog.message[0], "VizelVision") > 0, "notice names lock holder")
            Check(m.top.dialog.message[1] = "On air", "notice includes lock reason")
            Check(Instr(1, m.top.dialog.message[2], "Previous channel") > 0, "notice explains retained channel")
            Check(m.top.dialog.buttons.Count() = 1, "notice only has acknowledgment")
            OnLockNoticeChoice()
            Check(m.top.dialog.close, "OK closes notice")
            OnLockNoticeClosed()
            Check(m.top.focus, "dismissal gives the remote back to the picture")
            ' An old response must not replace the restored channel or notice.
            OnTune(7, {code: 200, data: {stream: {id: "stale"}}})
            Check(m.pending = invalid and m.watching.id = "previous", "late response is ignored")
        end for
    end for

    Setup("digital")
    OnTune(7, {code: 423, data: invalid})
    OnStatus({code: 0})
    Check(Instr(1, m.top.dialog.message[0], "Someone") > 0, "missing owner has a fallback")

    Setup("digital")
    m.lanes.digital.selection = {id: "nhk"}
    OnTune(7, {code: 423, data: {lock: {owner: "VizelVision"}}})
    OnStatus({code: 200, data: {lanes: m.lanes}})
    Check(m.pending = invalid, "shared channel completes tune")
    Check(m.watching.id = "nhk", "already showing requested channel watches along")
    Check(m.top.dialog = invalid, "watching along needs no notice")
    Check(m.bannerStatus = "Watching along with VizelVision", "sharing is explained")

    Setup("roku")
    OnTune(6, {code: 423, data: {lock: {owner: "Old owner"}}})
    Check(m.pending.phase = "tuning" and m.polls = 0, "superseded refusal is ignored")

    ' Turned on to a channel whose tuner is locked: channel 0 instead.
    Setup("digital")
    m.watching = {id: "", name: ""}
    OnTune(7, {code: 423, data: {lock: {owner: "VCR"}}})
    OnStatus({code: 200, data: {lanes: m.lanes}})
    Check(m.pending = invalid, "refusal at start is cleared")
    Check(m.watching.id = "vizelvision:0", "a locked channel at start falls back to channel 0")
    Check(m.top.dialog = invalid, "no dialog over the start")
    Check(m.bannerEntry.id = "vizelvision:0" and Instr(1, m.bannerStatus, "locked by VCR") > 0 and m.bannerAlert, "channel 0's banner says why")
    ' Or one that will not tune (yesterday's game).
    Setup("roku")
    m.watching = {id: "", name: ""}
    m.pending.phase = "tuning"
    OnTune(7, {code: 404, data: {error: {code: "not_found", message: "No such game"}}})
    Check(m.watching.id = "vizelvision:0", "a failed channel at start falls back to channel 0")
    Check(Instr(1, m.bannerStatus, "No such game") > 0, "channel 0's banner says what failed")
    ' While watching, a failure stays on what was on.
    Setup("roku")
    OnTune(7, {code: 404, data: {error: {code: "not_found", message: "No such game"}}})
    Check(m.watching.id = "previous" and m.bannerEntry.id = "nhk" and m.bannerAlert, "a failed change keeps the picture")

    ' Relays play at once: no tune, no lock, no status poll.
    Setup("digital")
    m.pending = invalid
    pentulive = {id: "internet:pentulive", kind: "internet", name: "Pentulive", key: "29", enabled: true, streamPath: "/stream/internet/pentulive/index.m3u8"}
    Choose(pentulive)
    Check(m.pending = invalid, "an internet stream needs no tune")
    Check(m.watching.id = "internet:pentulive" and m.watchLane = "", "an internet stream plays its relay")
    Check(m.requests.Count() = 0 and m.polls = 0, "an internet stream asks TV Thing nothing first")
    sportsGuide = {id: "slides:sports-guide", kind: "slides", name: "Sports Guide", key: "60", enabled: true, streamPath: "/stream/sports-guide/index.m3u8"}
    Choose(sportsGuide)
    Check(m.pending = invalid and m.watching.id = "slides:sports-guide", "the Sports Guide plays its video")
    Check(m.requests.Count() = 0, "the Sports Guide takes no tuner")
    Choose({id: "digital:nhk", kind: "digital", name: "NHK World", key: "44.1", enabled: true, streamPath: "", channelId: "nhk"})
    Check(m.pending <> invalid and m.pending.lane = "digital" and m.pending.phase = "checking", "an antenna channel is tuned")

    ' Why an internet stream will not play, in TV Thing's words.
    Setup("digital")
    m.watching = pentulive
    m.video = {state: "error"}
    m.probeFor = "internet:pentulive"
    OnProbe({code: 503, data: {error: "Yle Areena has nothing live for 1-78104441"}})
    Check(m.problem = "Yle Areena has nothing live for 1-78104441", "a 503 keeps the server's reason")
    Check(m.notice.visible and Instr(1, m.noticeText.text, "nothing live") > 0, "the notice says why")
    Check(m.bannerStatus = m.problem and m.bannerAlert, "the banner says why")
    Check(RetrySeconds() = 15, "a stream that is not live is asked again slowly")
    m.bannerStatus = ""
    m.probeFor = "internet:pentulive"
    OnProbe({code: 503, data: {error: "Yle Areena has nothing live for 1-78104441"}})
    Check(m.bannerStatus = "", "the same reason is not announced twice")
    m.probeFor = "internet:pentulive"
    m.watching = sportsGuide
    OnProbe({code: 404, data: {error: "Unknown internet stream"}})
    Check(m.problem = "Yle Areena has nothing live for 1-78104441", "a probe for another channel is ignored")
    Check(RelayProblem({code: 502, data: invalid}) = "Its source is not answering", "502 fallback")
    Check(RelayProblem({code: 403, data: invalid}) = "It will not play here", "403 fallback")
    Check(RelayProblem({code: 404, data: invalid}) = "TV Thing no longer has it", "404 fallback")
    Check(RelayProblem({code: 503, data: invalid}) = "Not on the air right now", "503 fallback")
    Check(RelayProblem({code: 0, data: invalid}) = "TV Thing did not answer", "no answer")
    Check(RelayProblem({code: 409, data: {error: {code: "busy", message: "Busy"}}}) = "Busy", "versioned error object")

    ' Off the air for long: back to channel 0, which itself keeps trying.
    Setup("digital")
    m.pending = invalid
    m.watching = pentulive
    m.problem = "Yle Areena has nothing live for 1-78104441"
    GiveUp()
    Check(m.watching.id = "vizelvision:0", "a dead channel hands over to channel 0")
    Check(Instr(1, m.bannerStatus, "Pentulive") > 0 and Instr(1, m.bannerStatus, "nothing live") > 0, "channel 0's banner says why")
    m.played = 0
    GiveUp()
    Check(m.watching.id = "vizelvision:0" and m.played = 1, "channel 0 keeps trying")
    m.catalog["vizelvision:0"] = {id: "vizelvision:0", kind: "vizelvision", name: "VizelVision", key: "0", enabled: true, streamPath: "/stream/vizelvision/index.m3u8", events: []}
    Check(ChannelZero().events <> invalid, "channel 0 comes from the lineup once it is in")

    ' A picture that stops without an error (TV Thing stopping, a playlist
    ' that stops moving) is reconnected after 15 s.
    Setup("digital")
    m.pending = invalid
    m.watching = pentulive
    m.video = {state: "playing", position: 5.0}
    m.lastPosition = 4.5
    CheckPlayback()
    Check(m.stallSince = -1, "a moving picture is left alone")
    CheckPlayback()
    Check(m.stallSince = 10, "a picture that stops moving is noticed")
    m.nowValue = 24
    CheckPlayback()
    Check(m.played = 0, "not before 15 s")
    m.nowValue = 25
    CheckPlayback()
    Check(m.played = 1 and m.notice.visible and m.requests.Count() = 1 and m.requests[0] = "probe", "a frozen picture reconnects and asks TV Thing why")
    m.video.state = "buffering"
    m.probeFor = ""
    m.nowValue = 30
    CheckPlayback()
    m.nowValue = 45
    CheckPlayback()
    Check(m.played = 2, "buffering for 15 s reconnects too")
    ' Retuning the lane on screen ends its stream on purpose.
    Setup("digital")
    m.watchLane = "digital"
    m.video = {state: "buffering", position: 0}
    CheckPlayback()
    m.nowValue = 60
    CheckPlayback()
    Check(m.played = 0, "a retune in progress is not a stall")
    ' TV Thing away (stopped for a deploy): keep trying the same channel.
    Setup("digital")
    m.pending = invalid
    m.watching = pentulive
    m.video = {state: "error", position: 0}
    m.probeFor = "internet:pentulive"
    OnProbe({code: 0, data: invalid})
    Check(m.serverAway and m.problem = "TV Thing did not answer", "no answer means TV Thing is away")
    Check(RetrySeconds() = 3, "TV Thing away is asked again quickly")
    GiveUp()
    Check(m.watching.id = "internet:pentulive" and m.played = 1, "TV Thing away never moves the channel to 0")
    ' A lane's playlist is asked about too.
    Setup("digital")
    m.pending = invalid
    m.watching = {id: "digital:wjla", name: "WJLA", kind: "digital", streamPath: ""}
    m.watchLane = "digital"
    Probe()
    Check(m.requestPaths.Count() = 1 and m.requestPaths[0] = "/stream/digital.m3u8", "a lane is probed at its playlist")

    ' The listings arriving refill the banner that is up (it starts before them).
    Setup("digital")
    m.banner = {visible: true}
    m.bannerShows = {id: "internet:cabride", status: "Back to live", alert: false}
    m.catalog["internet:cabride"] = {id: "internet:cabride", name: "Cab Ride", events: [{title: "Bergen Line"}]}
    RefreshBanner()
    Check(m.filled <> invalid and m.filled.events.Count() = 1 and m.filledStatus = "Back to live", "the banner up takes the listings, status and all")
    m.filled = invalid
    m.banner.visible = false
    RefreshBanner()
    Check(m.filled = invalid, "a hidden banner is left alone")

    ' The remote: OK brings up the guide; Left and Right change channel.
    for each case in [["OK", "OpenGuide"], ["right", "Surf(1)"], ["left", "Surf(-1)"], ["up", "Surf(1)"], ["down", "Surf(-1)"], ["back", "Banner"]]
        Keys(false)
        handled = onKeyEvent(case[0], true)
        Check(handled and m.calls = case[1], "watching, " + case[0] + " is " + case[1] + " (got " + m.calls + ")")
    end for
    for each case in [["OK", "GuideSelect"], ["back", "CloseGuide"], ["left", "GuideStep(-1)"], ["right", "GuideStep(1)"], ["up", "GuideMove(-1)"], ["down", "GuideMove(1)"], ["fastforward", "GuidePage(10)"], ["rewind", "GuidePage(-10)"]]
        Keys(true)
        handled = onKeyEvent(case[0], true)
        Check(handled and m.calls = case[1], "in the guide, " + case[0] + " is " + case[1] + " (got " + m.calls + ")")
    end for
    Keys(false)
    Check(not onKeyEvent("right", false) and m.calls = "", "a key's release does nothing")
    Keys(false)
    m.previous = {id: "before", name: "Before", kind: "internet", enabled: true, streamPath: "/stream/internet/before/index.m3u8"}
    Check(onKeyEvent("replay", true) and m.watching.id = "before", "Replay goes back to the channel before")

    ' The pool relays the channel from another box's tuner (NHK, 2026-11-10):
    ' follow that tuner, not this node's digital lane, which shows something else.
    Setup("digital")
    peerIdle = {id: "viviserv2:digital", selection: invalid, stream: {id: invalid, ready: false, playlist_path: "/stream/peer/digital.m3u8"}}
    OnTune(7, {code: 200, data: {stream: {id: "peer-gen"}, tuner: "viviserv2:digital", lane: "viviserv2:digital", relayed: true}})
    Check(m.pending.lane = "viviserv2:digital" and m.pending.phase = "starting", "relayed tune follows the peer tuner")
    OnStatus({code: 200, data: {node: "viviserv", lanes: m.lanes, tuners: [peerIdle]}})
    Check(m.pending <> invalid and m.bannerStatus = "", "a peer listing a poll behind is waited for")
    peerReady = {id: "viviserv2:digital", selection: {id: "nhk"}, stream: {id: "peer-gen", ready: true, playlist_path: "/stream/peer/digital.m3u8"}}
    OnStatus({code: 200, data: {node: "viviserv", lanes: m.lanes, tuners: [peerReady]}})
    Check(m.pending = invalid and m.watching.id = "nhk", "relayed tune completes")
    Check(m.watchLane = "viviserv2:digital" and m.watchStream = "peer-gen", "watches the relayed tuner")
    Check(LanePath(m.watchLane) = "/stream/peer/digital.m3u8", "plays the relay path")

    Setup("digital")
    OnTune(7, {code: 200, data: {stream: {id: "peer-gen"}, tuner: "viviserv2:digital", relayed: true}})
    m.nowValue = 25
    OnStatus({code: 200, data: {node: "viviserv", lanes: m.lanes, tuners: [{id: "viviserv2:digital", selection: {id: "wjla"}, stream: {id: "theirs", ready: true}}]}})
    Check(m.pending = invalid and m.bannerStatus = "Someone else changed the channel", "a peer tuner retuned by someone else still says so")

    Setup("digital")
    OnTune(7, {code: 200, data: {stream: {id: "peer-gen"}, tuner: "viviserv2:digital", relayed: true}})
    m.nowValue = 45
    OnStatus({code: 200, data: {node: "viviserv", lanes: m.lanes, tuners: []}})
    Check(m.pending = invalid and m.bannerStatus = "NHK World did not start", "a relayed tuner that vanishes gives up")

    Setup("digital")
    m.lanes.digital = {selection: {id: "nhk"}, stream: {id: "here-gen", ready: true}}
    OnStatus({code: 200, data: {node: "viviserv", lanes: m.lanes, tuners: []}})
    m.pending.phase = "tuning"
    OnTune(7, {code: 200, data: {stream: {id: "here-gen"}, tuner: "viviserv:digital", relayed: false}})
    Check(m.pending.lane = "digital", "this node's own tuner stays the digital lane")
    OnStatus({code: 200, data: {node: "viviserv", lanes: m.lanes, tuners: []}})
    Check(m.pending = invalid and m.watchLane = "digital" and m.watchStream = "here-gen", "a local tune completes on the digital lane")

    Setup("digital")
    m.node = "viviserv"
    m.tuners = [{id: "viviserv2:digital", selection: {id: "nhk"}, stream: {id: "held", ready: true}}]
    OnTune(7, {code: 423, data: {lock: {owner: "VizelVision master"}, tuner: "viviserv2:digital"}})
    OnStatus({code: 200, data: {node: "viviserv", lanes: m.lanes, tuners: m.tuners}})
    Check(m.watching.id = "nhk" and m.watchLane = "viviserv2:digital", "a locked peer tuner showing it is watched along with")
    print "SCENE SCENARIOS PASSED"
end sub

sub Setup(lane as String)
    m.serial = 7
    m.pending = {entry: {id: "nhk", name: "NHK World"}, serial: 7, lane: lane, phase: "tuning", lock: invalid, operationId: ""}
    m.watching = {id: "previous", name: "Previous channel"}
    m.watchLane = ""
    m.watchStream = "original"
    m.lanes = {}
    m.lanes[lane] = {selection: {id: "different"}, stream: {id: "live", ready: true}}
    m.tuners = invalid
    m.node = ""
    m.top = {dialog: invalid, focus: false, setFocus: sub(value)
        m.focus = value
    end sub}
    m.guide = {visible: false}
    m.surfTimer = {control: "start"}
    m.surfIndex = 9
    m.statusAgain = false
    m.statusAt = -100
    m.polls = 0
    m.played = 0
    m.requests = []
    m.guideNotice = ""
    m.bannerEntry = invalid
    m.bannerStatus = ""
    m.bannerAlert = false
    m.problem = ""
    m.probeFor = ""
    m.unplayableSince = -1
    m.followedOperation = ""
    m.serverAway = false
    m.stallSince = -1
    m.lastPosition = -1
    m.nowValue = 10
    m.requestPaths = []
    m.video = {state: "playing", position: 0}
    m.notice = {visible: false}
    m.noticeText = {text: ""}
    m.pipSlate = {text: ""}
    m.catalog = {}
end sub

' Watching, or in the guide, with a channel before this one.
sub Keys(inGuide as Boolean)
    Setup("digital")
    m.guide.visible = inGuide
    m.gridRows = 10
    m.chosenOk = false
    m.previous = {id: "before", name: "Before"}
    m.calls = ""
end sub

sub Check(ok as Boolean, message as String)
    if not ok then print "FAIL: " + message
end sub

function TestDialog() as Object
    return {observeField: sub(field, callback)
    end sub}
end function

sub PollNow()
    m.polls = m.polls + 1
end sub

sub Request(tag, method, path, body, timeoutMs)
    m.requests.Push(tag)
    m.requestPaths.Push(path)
end sub

sub MarkCurrent()
end sub

sub FollowLane()
end sub

function EntryById(id as String) as Dynamic
    return m.catalog[id]
end function

function LineupChannelZero() as Object
    return {id: "vizelvision:0", kind: "vizelvision", name: "VizelVision", key: "0", enabled: true, streamPath: "/stream/vizelvision/index.m3u8"}
end function

sub ShowBanner(entry, status, seconds, alert = false)
    m.bannerEntry = entry
    m.bannerStatus = status
    m.bannerAlert = alert
    if m.calls <> invalid and m.calls = "" then m.calls = "Banner"
end sub

sub FillBanner(entry, status, alert)
    m.filled = entry
    m.filledStatus = status
end sub

sub ShowPending(text)
    m.pending.text = text
end sub

sub SendTune()
    m.requests.Push("tune")
end sub

function TuningText(entry) as String
    return "Tuning"
end function

sub Watch(entry, lane, stream)
    m.watching = entry
    m.watchLane = lane
    m.watchStream = stream
end sub

sub SetGuideNotice(text)
    m.guideNotice = text
end sub

sub NoteChosenOk()
end sub

sub PlayUrl(url)
    m.played = m.played + 1
end sub

function WatchUrl() as String
    return "http://tv/stream"
end function

sub OpenGuide()
    m.calls = "OpenGuide"
end sub

sub CloseGuide()
    m.calls = "CloseGuide"
end sub

sub GuideSelect()
    m.calls = "GuideSelect"
end sub

sub GuideMove(delta)
    m.calls = "GuideMove(" + delta.ToStr() + ")"
end sub

sub GuideStep(delta)
    m.calls = "GuideStep(" + delta.ToStr() + ")"
end sub

sub GuidePage(delta)
    m.calls = "GuidePage(" + delta.ToStr() + ")"
end sub

sub Surf(delta)
    m.calls = "Surf(" + delta.ToStr() + ")"
end sub

function Now() as Integer
    return m.nowValue
end function

function LineupSelectionId(lane, selection) as String
    if not LineupIsObject(selection) then return ""
    return selection.id
end function

function LineupLaneKind(lane as String) as String
    if Instr(1, lane, ":") > 0 then return "digital"
    return lane
end function

function LineupText(value) as String
    if type(value) = "roString" or type(value) = "String" then return value
    return ""
end function

function LineupIsObject(value) as Boolean
    return type(value) = "roAssociativeArray"
end function

function LineupIsTrue(value) as Boolean
    return value = true
end function
