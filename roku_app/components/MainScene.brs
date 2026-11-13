' TV Thing on Roku, as a cable box: the website's look (static/guide.html
' and the Watch page's info banner), the Watch page's lineup.
'
' It starts on the channel watched last, or channel 0, with no keypress:
' the living-room TV opens it when it is turned on. Left and Right (and Up
' and Down) change channel, OK brings up the guide, and in the guide OK
' watches the highlighted channel and Back leaves it.
'
' Antenna channels tune TV Thing's digital lane, or whichever tuner of its
' pool TV Thing picks: one on another box plays here relayed (/stream/peer/),
' followed by its id ("viviserv2:digital"). YouTube TV channels and
' games put the source Roku on them, which takes a minute or two while it
' navigates; the old picture stays up until the new one is verified. Internet
' streams, the Sports Guide and VizelVision play TV Thing's relay of them:
' nothing is tuned. The app takes no lock. When someone holds the lane and it
' already shows the channel, it watches along; otherwise it stays on what was
' on (or goes to channel 0) and explains who holds the tuner. Channel
' changes never override a lock.

sub init()
    m.top.backExitsScene = false
    m.top.backgroundUri = ""
    m.top.backgroundColor = "0x050F2BFF"
    m.video = m.top.findNode("video")
    m.under = m.top.findNode("under")
    m.pipSlate = m.top.findNode("pipSlate")
    m.notice = m.top.findNode("notice")
    m.noticeText = m.top.findNode("noticeText")
    m.banner = m.top.findNode("banner")
    m.bnNum = m.top.findNode("bnNum")
    m.bnNumShadow = m.top.findNode("bnNumShadow")
    m.bnCall = m.top.findNode("bnCall")
    m.bnSub = m.top.findNode("bnSub")
    m.bnTitle = m.top.findNode("bnTitle")
    m.bnTitleShadow = m.top.findNode("bnTitleShadow")
    m.bnTime = m.top.findNode("bnTime")
    m.bnBar = m.top.findNode("bnBar")
    m.bnFill = m.top.findNode("bnFill")
    m.bnDesc = m.top.findNode("bnDesc")
    m.bnStatus = m.top.findNode("bnStatus")
    m.bnLater = m.top.findNode("bnLater")
    m.bnClock = m.top.findNode("bnClock")
    m.bnClockShadow = m.top.findNode("bnClockShadow")
    m.bnAmPm = m.top.findNode("bnAmPm")
    m.bnDate = m.top.findNode("bnDate")
    m.later = []
    for i = 0 to 2
        name = "later" + i.ToStr()
        m.later.Push({group: m.top.findNode(name), time: m.top.findNode(name + "Time"), title: m.top.findNode(name + "Title")})
    end for
    m.guide = m.top.findNode("guide")
    m.pipBug = m.top.findNode("pipBug")
    m.infoChan = m.top.findNode("infoChan")
    m.infoBadge = m.top.findNode("infoBadge")
    m.infoBadgeText = m.top.findNode("infoBadgeText")
    m.infoTitle = m.top.findNode("infoTitle")
    m.infoTitleShadow = m.top.findNode("infoTitleShadow")
    m.infoTime = m.top.findNode("infoTime")
    m.infoDesc = m.top.findNode("infoDesc")
    m.gClock = m.top.findNode("gClock")
    m.gClockShadow = m.top.findNode("gClockShadow")
    m.gAmPm = m.top.findNode("gAmPm")
    m.gDate = m.top.findNode("gDate")
    m.gDay = m.top.findNode("gDay")
    m.gNow = m.top.findNode("gNow")
    m.gStatus = m.top.findNode("gStatus")
    m.ticks = []
    for i = 0 to 3
        m.ticks.Push(m.top.findNode("gTick" + i.ToStr()))
    end for
    m.rows = []
    for i = 0 to 9
        m.rows.Push(m.top.findNode("row" + i.ToStr()))
    end for
    m.surfTimer = m.top.findNode("surfTimer")
    m.bannerTimer = m.top.findNode("bannerTimer")
    m.retryTimer = m.top.findNode("retryTimer")
    m.tickTimer = m.top.findNode("tickTimer")
    m.lineupTimer = m.top.findNode("lineupTimer")

    ' The grid: 10 rows, two hours across 1058 px, six hours ahead at most.
    m.gridRows = 10
    m.gridSpan = 7200
    m.gridWidth = 1058
    m.gridHorizon = 6 * 3600

    m.clock = CreateObject("roTimespan")
    m.watching = NoChannel() ' what is on screen; id "" is nothing yet
    m.previous = invalid     ' the channel before it (Replay goes back)
    m.watchLane = ""         ' "digital", "roku" or a pool tuner's id while watching a lane
    m.watchStream = ""       ' the lane generation the player is attached to
    m.pending = invalid      ' a tune on its way (see Choose)
    m.serial = 0
    m.lanes = invalid        ' api/v1/status lanes, as last seen
    m.tuners = invalid       ' api/v1/status tuners (the antenna pool), as last seen
    m.node = ""              ' the TV Thing node the app talks to
    m.statusAt = -100
    m.statusBusy = false
    m.statusAgain = false
    m.lastPoll = -100
    m.unplayableSince = -1
    m.problem = ""           ' why a stream will not play, as TV Thing said
    m.serverAway = false     ' TV Thing itself did not answer
    m.stallSince = -1        ' when the picture stopped moving without an error
    m.lastPosition = -1
    m.probeFor = ""          ' the channel a probe is asking about
    m.guideNotice = ""       ' what went wrong, for the guide's status line
    m.surfIndex = -1
    m.currentId = ""
    m.followedOperation = ""
    m.entries = []
    m.indexById = {}
    m.lineupTask = invalid
    m.lineupError = ""
    m.keptLast = false
    m.lineupLoaded = false
    m.savedId = ""
    m.bannerShows = invalid  ' {id, status, alert} of the banner's channel
    m.gIndex = 0             ' the guide's highlighted channel
    m.gTop = 0               ' its first row on screen
    m.gEarliest = 0          ' the half hour now began: the grid's left limit
    m.gStart = 0             ' the window's start (half-hour steps)
    m.gTime = 0              ' the time the highlight follows up and down
    m.minuteShown = ""
    m.tasks = {}
    m.taskSeq = 0
    m.chosenOk = false       ' an OK just chose a dialog button
    m.chosenClock = CreateObject("roTimespan")

    m.video.observeField("state", "OnVideoState")
    m.surfTimer.observeField("fire", "OnSurfTimer")
    m.bannerTimer.observeField("fire", "OnBannerTimer")
    m.retryTimer.observeField("fire", "OnRetry")
    m.tickTimer.observeField("fire", "OnTick")
    m.lineupTimer.observeField("fire", "LoadLineup")

    UpdateClock()
    m.top.setFocus(true)
end sub

' Straight into the channel watched last, or channel 0.
sub OnServerUrl()
    entry = SavedChannel()
    if entry = invalid then entry = LineupChannelZero()
    Choose(entry)
    LoadLineup()
    m.tickTimer.control = "start"
    m.lineupTimer.control = "start"
end sub

function NoChannel() as Object
    return LineupEntry("", "none", "", "", "", "")
end function

' Channel 0 from the lineup once it is in (the same entry otherwise).
function ChannelZero() as Object
    entry = EntryById("vizelvision:0")
    if entry = invalid then entry = LineupChannelZero()
    return entry
end function

' ── Watching ─────────────────────────────────────────────────────

' Puts entry on screen: TV Thing's relay of it, or a lane's playlist for the
' generation that was tuned.
sub Watch(entry as Object, lane as String, stream as String)
    ' The Roku's capture never stops: a channel it navigated to is already in
    ' the playing stream, so only a different lane or generation reattaches.
    attached = lane <> "" and lane = m.watchLane and stream = m.watchStream and m.video.state = "playing"
    if m.watching.id <> "" and m.watching.id <> entry.id then m.previous = m.watching
    m.watching = entry
    m.watchLane = lane
    m.watchStream = stream
    m.unplayableSince = -1
    m.problem = ""
    m.serverAway = false
    m.stallSince = -1
    m.guideNotice = ""
    if not attached then PlayUrl(WatchUrl())
    MarkCurrent()
    SaveChannel(entry)
    ShowBanner(entry, "", 6)
end sub

function WatchUrl() as String
    ' VizelVision, the Sports Guide and internet streams: TV Thing's relay.
    if m.watching.streamPath <> "" then return m.top.serverUrl + m.watching.streamPath
    if m.watchLane = "" then return ""
    ' A new URL per generation, so the player never keeps a stale timeline.
    return m.top.serverUrl + LanePath(m.watchLane) + "?v=" + m.watchStream
end function

sub PlayUrl(url as String)
    m.video.control = "stop"
    if url = "" then return
    content = CreateObject("roSGNode", "ContentNode")
    content.url = url
    content.streamFormat = "hls"
    content.live = true
    m.video.content = content
    m.video.control = "play"
end sub

' Full screen, or in the guide's preview box.
sub PlaceVideo(preview as Boolean)
    if preview
        m.video.translation = [20, 20]
        m.video.width = 320
        m.video.height = 180
    else
        m.video.translation = [0, 0]
        m.video.width = 1280
        m.video.height = 720
    end if
end sub

sub OnVideoState()
    if m.watching.id = "" then return
    state = m.video.state
    if state = "playing"
        m.notice.visible = false
        m.unplayableSince = -1
        m.problem = ""
    else if state = "error" or state = "finished"
        if state = "error"
            print "[TV Thing] video error "; m.video.errorCode; " "; m.video.errorMsg; " "; m.video.errorStr
        end if
        if m.unplayableSince < 0 then m.unplayableSince = Now()
        ' Retuning the lane on screen ends its old stream; the banner already says so.
        if m.pending = invalid or m.pending.lane <> m.watchLane then ShowNotice()
        ' The player only says it failed; TV Thing says why.
        Probe()
        m.retryTimer.duration = RetrySeconds()
        m.retryTimer.control = "start"
    end if
end sub

sub ShowNotice()
    if m.problem <> ""
        m.noticeText.text = m.watching.name + ": " + m.problem + " · trying again…"
    else
        m.noticeText.text = m.watching.name + " is unavailable · reconnecting…"
    end if
    ' In the guide, the preview box says it instead.
    m.notice.visible = not m.guide.visible
    m.pipSlate.text = "NO SIGNAL"
end sub

'An internet stream that is not live is asked again every 15 s, not 3.
function RetrySeconds() as Integer
    if m.watching.kind = "internet" and m.problem <> "" and not m.serverAway then return 15
    return 3
end function

sub OnRetry()
    if m.watching.id = "" then return
    state = m.video.state
    if state = "playing" or state = "buffering" then return
    if m.unplayableSince >= 0 and Now() - m.unplayableSince > 90
        GiveUp()
        return
    end if
    lane = LaneStatus(m.watchLane)
    if lane <> invalid
        stream = StreamId(lane.stream)
        if stream <> "" then m.watchStream = stream
    end if
    PlayUrl(WatchUrl())
end sub

' A channel off the air for a minute and a half goes back to channel 0,
' with the reason. Channel 0 itself keeps trying, and so does any channel
' while TV Thing is away (restarting, or stopped for a deploy): the picture
' comes back on its own when TV Thing does.
sub GiveUp()
    zero = ChannelZero()
    if m.watching.id = zero.id or m.serverAway
        m.unplayableSince = Now()
        PlayUrl(WatchUrl())
        return
    end if
    reason = m.problem
    if reason = "" then reason = "went off the air"
    message = m.watching.name + ": " + reason
    Choose(zero)
    ShowBanner(zero, message, 8, true)
end sub

sub Probe()
    if m.probeFor <> "" then return
    m.probeFor = m.watching.id
    path = m.watching.streamPath
    if path = "" then path = LanePath(m.watchLane)
    Request("probe", "GET", path, invalid, 15000)
end sub

sub OnProbe(r as Object)
    target = m.probeFor
    m.probeFor = ""
    if target <> m.watching.id then return
    m.serverAway = r.code = 0
    problem = ""
    if r.code <> 200 then problem = RelayProblem(r)
    changed = problem <> m.problem
    m.problem = problem
    if m.video.state = "playing" then return
    ShowNotice()
    if changed and problem <> "" then ShowBanner(m.watching, problem, 8, true)
end sub

' The player does not always say when a stream dies: TV Thing stopping or
' restarting, or a playlist that stops moving, can leave it buffering, or
' showing the last frame as if playing, with no error. Anything but a
' moving picture for 15 s reconnects, as an error would.
sub CheckPlayback()
    state = m.video.state
    position = m.video.position
    moving = state = "playing" and position <> m.lastPosition
    m.lastPosition = position
    ' Retuning the lane on screen ends its old stream on purpose.
    retuning = m.pending <> invalid and m.pending.lane = m.watchLane and m.watchLane <> ""
    ' An error or the end of the stream is the retry timer's.
    if m.watching.id = "" or moving or retuning or state = "error" or state = "finished"
        m.stallSince = -1
        return
    end if
    if m.stallSince < 0
        m.stallSince = Now()
    else if Now() - m.stallSince >= 15
        print "[TV Thing] no picture for 15 s ("; state; "), reconnecting"
        m.stallSince = -1
        Reconnect()
    end if
end sub

sub Reconnect()
    if m.unplayableSince < 0 then m.unplayableSince = Now()
    if m.unplayableSince >= 0 and Now() - m.unplayableSince > 90
        GiveUp()
        return
    end if
    ShowNotice()
    Probe()
    lane = LaneStatus(m.watchLane)
    if lane <> invalid
        stream = StreamId(lane.stream)
        if stream <> "" then m.watchStream = stream
    end if
    PlayUrl(WatchUrl())
end sub

' ── Tuning ───────────────────────────────────────────────────────

' m.pending: {entry, serial, lane, phase, stream, operationId, lock, started,
' since, text}. Its phase is
'   checking    reading the lane before tuning it
'   tuning      POST api/v1/tune on its way
'   locked      refused (423): does the holder already show it?
'   navigating  the Roku is finding it (a 202 operation)
'   starting    waiting for the tuned generation to be ready
sub Choose(entry as Object)
    m.serial = m.serial + 1
    m.pending = invalid
    m.guideNotice = ""
    if not entry.enabled
        ShowBanner(entry, "Not available now", 4, true)
        return
    end if
    if entry.streamPath <> ""
        Watch(entry, "", "")
        return
    end if
    lane = LaneOf(entry)
    m.pending = {entry: entry, serial: m.serial, lane: lane, phase: "checking", stream: "", operationId: "", lock: invalid, started: Now(), since: Now(), text: ""}
    if Now() - m.statusAt > 5
        ShowPending(TuningText(entry))
        PollNow()
    else
        SendTune()
    end if
end sub

function LaneOf(entry as Object) as String
    if entry.kind = "digital" then return "digital"
    if entry.kind = "roku" then return "roku"
    return ""
end function

sub SendTune()
    p = m.pending
    entry = p.entry
    if p.lane = "digital"
        body = {source: "digital", channel_id: entry.channelId, quality: DigitalQuality()}
    else
        body = {source: entry.sourceId, channel_id: entry.channelId}
        if entry.date <> "" then body.date = entry.date
    end if
    p.phase = "tuning"
    ShowPending(TuningText(entry))
    Request("tune", "POST", "/api/v1/tune", body, 45000)
end sub

' The digital lane is shared: keep the quality it already runs at, so a
' channel change never also re-encodes it for everyone else.
function DigitalQuality() as String
    lane = LaneStatus("digital")
    if lane <> invalid
        quality = LineupText(lane.quality)
        if quality = "low" or quality = "medium" or quality = "high" then return quality
    end if
    return "medium"
end function

function TuningText(entry as Object) as String
    if entry.kind = "digital" then return "Tuning channel " + entry.key + "…"
    return "Asking the Roku for " + entry.name + "…"
end function

sub OnTune(serial as Integer, r as Object)
    p = m.pending
    if p = invalid or p.serial <> serial then return
    data = r.data
    if (r.code = 200 or r.code = 202 or r.code = 423) and LineupIsObject(data)
        p.lane = TunedLane(data, p.lane)
    end if
    if (r.code = 200 or r.code = 202) and LineupIsObject(data)
        p.stream = StreamId(data.stream)
        operation = data.operation
        if r.code = 202 and LineupIsObject(operation)
            p.phase = "navigating"
            p.operationId = LineupText(operation.id)
            ShowPending(NavigationText(operation))
        else
            p.phase = "starting"
            p.since = Now()
        end if
        PollNow()
    else if r.code = 423
        if LineupIsObject(data) then p.lock = data.lock
        p.phase = "locked"
        PollNow()
    else
        Fail(TuneProblem(r))
    end if
end sub

' Moves a pending tune along with each status poll.
sub AdvancePending()
    p = m.pending
    if p = invalid then return
    entry = p.entry
    lane = LaneStatus(p.lane)
    if lane = invalid
        if p.phase = "locked" then ReturnFromLockedTune()
        ' A relayed tuner whose box stopped answering.
        if p.phase = "starting" and Now() - p.since > 30 then Fail(entry.name + " did not start")
        return
    end if
    if p.phase = "checking"
        SendTune()
        return
    end if
    if p.phase = "locked"
        if LineupSelectionId(p.lane, lane.selection) = entry.id and StreamReady(lane)
            ' The holder already shows it: watch along, nothing changes.
            m.pending = invalid
            Watch(entry, p.lane, StreamId(lane.stream))
            ShowBanner(entry, "Watching along with " + LockOwner(p.lock), 6)
        else
            ReturnFromLockedTune()
        end if
        return
    end if
    if p.phase = "navigating"
        operation = invalid
        if LineupIsObject(lane.roku) then operation = lane.roku.operation
        if not LineupIsObject(operation) or LineupText(operation.id) <> p.operationId
            Fail("Someone else moved the Roku first")
            ' Said just now, so FollowLane need not announce it again.
            if LineupIsObject(operation) then m.followedOperation = LineupText(operation.id)
            return
        end if
        state = LineupText(operation.state)
        if state = "failed" or state = "cancelled"
            Fail(OperationProblem(operation))
            return
        end if
        if state <> "succeeded"
            if Now() - p.started > 420
                Fail("The Roku is still looking after seven minutes")
            else
                ShowPending(NavigationText(operation) + "  ·  " + Elapsed(p.started))
            end if
            return
        end if
        p.phase = "starting"
        p.since = Now()
        p.stream = StreamId(lane.stream)
    end if
    if p.phase = "starting"
        stream = lane.stream
        if LineupIsObject(stream) and LineupText(stream.state) = "failed"
            Fail(StreamProblem(stream))
            return
        end if
        id = StreamId(stream)
        if id <> p.stream
            if LineupSelectionId(p.lane, lane.selection) <> entry.id
                ' Another box's tuner is listed as TV Thing last read it, a
                ' poll or two behind the tune it just relayed.
                if IsPoolTuner(p.lane) and Now() - p.since < 10 then return
                Fail("Someone else changed the channel")
                return
            end if
            p.stream = id   ' recovery restarted it on the same channel
        end if
        if id <> "" and StreamReady(lane)
            m.pending = invalid
            Watch(entry, p.lane, id)
        else if Now() - p.since > 30
            Fail(entry.name + " did not start")
        end if
    end if
end sub

sub ReturnFromLockedTune()
    p = m.pending
    if p = invalid then return
    owner = LockOwner(p.lock)
    tuner = "source Roku"
    if LineupLaneKind(p.lane) = "digital" then tuner = "antenna tuner"

    ' The refused tune never changed playback. Restore the channel without
    ' retuning or restarting the video (which could interrupt a holder).
    m.pending = invalid
    m.serial = m.serial + 1
    m.surfTimer.control = "stop"
    m.surfIndex = -1
    MarkCurrent()
    if m.watching.id = ""
        ' Starting up on it: channel 0 instead, and say why on its banner.
        zero = ChannelZero()
        Choose(zero)
        ShowBanner(zero, p.entry.name + ": the " + tuner + " is locked by " + owner, 8, true)
        return
    end if
    ShowBanner(m.watching, "Tuner locked by " + owner, 8, true)

    message = ["Cannot switch to " + p.entry.name + ". " + owner + " has the " + tuner + " locked."]
    if LineupIsObject(p.lock)
        reason = LineupText(p.lock.reason)
        if reason <> "" then message.Push(reason)
    end if
    message.Push("Still watching " + m.watching.name + ". Try again when the tuner is free.")
    dialog = CreateObject("roSGNode", "StandardMessageDialog")
    dialog.title = "Channel unavailable"
    dialog.message = message
    dialog.buttons = ["OK"]
    dialog.observeField("buttonSelected", "OnLockNoticeChoice")
    dialog.observeField("wasClosed", "OnLockNoticeClosed")
    m.top.dialog = dialog
end sub

sub OnLockNoticeChoice()
    dialog = m.top.dialog
    if dialog = invalid then return
    NoteChosenOk()
    dialog.close = true
end sub

sub OnLockNoticeClosed()
    m.top.setFocus(true)
end sub

sub Fail(message as String)
    entry = m.watching
    if m.pending <> invalid
        entry = m.pending.entry
        ' A navigation of ours that is given up on is not "someone moving the Roku".
        if m.pending.operationId <> "" then m.followedOperation = m.pending.operationId
    end if
    m.pending = invalid
    SetGuideNotice(entry.name + ": " + message)
    if m.watching.id = ""
        ' Starting up on it: channel 0 instead, and say why on its banner.
        zero = ChannelZero()
        Choose(zero)
        ShowBanner(zero, entry.name + ": " + message, 8, true)
    else
        ShowBanner(entry, message, 8, true)
    end if
end sub

' ── Status ───────────────────────────────────────────────────────

sub PollNow()
    if m.statusBusy
        m.statusAgain = true
        return
    end if
    m.statusBusy = true
    m.lastPoll = Now()
    Request("status", "GET", "/api/v1/status", invalid, 8000)
end sub

sub OnStatus(r as Object)
    m.statusBusy = false
    if r.code = 200 and LineupIsObject(r.data) and LineupIsObject(r.data.lanes)
        m.lanes = r.data.lanes
        m.tuners = r.data.tuners
        m.node = LineupText(r.data.node)
        m.statusAt = Now()
        MarkCurrent()
        AdvancePending()
        FollowLane()
    else if m.pending <> invalid
        phase = m.pending.phase
        if phase = "checking"
            SendTune()
        else if phase = "locked"
            ReturnFromLockedTune()
        end if
    end if
    if m.statusAgain
        m.statusAgain = false
        PollNow()
    end if
end sub

' While watching a lane: reattach to a new generation, and follow (and say)
' a channel someone else changed it to.
sub FollowLane()
    if m.watchLane = "" then return
    p = m.pending
    if p <> invalid and p.lane = m.watchLane then return
    lane = LaneStatus(m.watchLane)
    if lane = invalid then return
    id = StreamId(lane.stream)
    if id <> "" and id <> m.watchStream and StreamReady(lane)
        m.watchStream = id
        PlayUrl(WatchUrl())
    end if
    selectionId = LineupSelectionId(m.watchLane, lane.selection)
    if selectionId <> "" and selectionId <> m.watching.id
        entry = EntryById(selectionId)
        if entry = invalid then entry = SelectionEntry(m.watchLane, lane.selection, selectionId)
        m.watching = entry
        SaveChannel(entry)
        ShowBanner(entry, "Changed on TV Thing", 6)
    end if
    if m.watchLane = "roku" and LineupIsObject(lane.roku) and LineupIsObject(lane.roku.operation)
        operation = lane.roku.operation
        id = LineupText(operation.id)
        if LineupIsTrue(operation.active) and id <> m.followedOperation
            m.followedOperation = id
            ShowBanner(m.watching, "Someone is moving the Roku: " + LineupText(operation.message), 8)
        end if
    end if
end sub

' The channel on screen, as the guide marks it.
sub MarkCurrent()
    currentId = m.watching.id
    lane = LaneStatus(m.watchLane)
    if lane <> invalid
        id = LineupSelectionId(m.watchLane, lane.selection)
        if id <> "" then currentId = id
    end if
    if currentId = m.currentId then return
    m.currentId = currentId
    if m.guide.visible then RenderGuide()
end sub

' ── The channel watched last ─────────────────────────────────────

function SavedChannel() as Dynamic
    section = CreateObject("roRegistrySection", "tvthing")
    if not section.Exists("channel") then return invalid
    saved = ParseJson(section.Read("channel"))
    if not LineupIsObject(saved) or LineupText(saved.id) = "" then return invalid
    entry = LineupEntry(LineupText(saved.id), LineupText(saved.kind), LineupText(saved.name), LineupText(saved.key), LineupText(saved.detail), "")
    entry.label = LineupText(saved.label)
    entry.sourceId = LineupText(saved.sourceId)
    entry.channelId = LineupText(saved.channelId)
    entry.date = LineupText(saved.date)
    entry.streamPath = LineupText(saved.streamPath)
    if LineupIsNumber(saved.number) then entry.number = saved.number
    m.savedId = entry.id
    return entry
end function

sub SaveChannel(entry as Object)
    if entry.id = "" or entry.id = m.savedId then return
    m.savedId = entry.id
    saved = {id: entry.id, kind: entry.kind, name: entry.name, key: entry.key, detail: entry.detail, label: entry.label, sourceId: entry.sourceId, channelId: entry.channelId, date: entry.date, streamPath: entry.streamPath, number: entry.number}
    section = CreateObject("roRegistrySection", "tvthing")
    section.Write("channel", FormatJson(saved))
    section.Flush()
end sub

' ── Lineup ───────────────────────────────────────────────────────

sub LoadLineup()
    if m.lineupTask <> invalid or m.top.serverUrl = "" then return
    task = CreateObject("roSGNode", "LineupTask")
    task.serverUrl = m.top.serverUrl
    task.date = LineupLocalDate()
    task.hours = m.gridHorizon \ 3600
    task.observeField("result", "OnLineup")
    m.lineupTask = task
    task.control = "run"
end sub

sub OnLineup()
    result = m.lineupTask.result
    m.lineupTask = invalid
    m.lineupError = LineupText(result.error)
    entries = result.entries
    if type(entries) <> "roArray" then entries = []
    ' TV Thing away, or its antenna list missing: keep the channels it last listed.
    m.keptLast = entries.Count() = 0 or (not LineupIsTrue(result.complete) and m.lineupLoaded)
    if not m.keptLast then ApplyLineup(entries)
    ' Ask again sooner while something is missing.
    seconds = 180
    if m.lineupError <> "" then seconds = 15
    if m.lineupTimer.duration <> seconds
        m.lineupTimer.control = "stop"
        m.lineupTimer.duration = seconds
        m.lineupTimer.control = "start"
    end if
    UpdateGuideStatus()
end sub

sub ApplyLineup(entries as Object)
    focusedId = ""
    if m.guide.visible and m.gIndex < m.entries.Count() then focusedId = m.entries[m.gIndex].id
    index = {}
    for i = 0 to entries.Count() - 1
        index[entries[i].id] = i
    end for
    m.entries = entries
    m.indexById = index
    if entries.Count() > 0 then m.lineupLoaded = true
    RefreshWatching()
    RefreshBanner()
    MarkCurrent()
    if m.guide.visible
        ' The highlight stays on its channel while the list changes around it.
        i = IndexOf(focusedId)
        if i < 0 then i = IndexOf(m.currentId)
        if i < 0 then i = 0
        m.gIndex = i
        KeepInView()
        RenderGuide()
    end if
end sub

' The listings and numbers on screen come from the lineup once it is in.
sub RefreshWatching()
    if m.watching.id = "" then return
    fresh = EntryById(m.watching.id)
    if fresh <> invalid then m.watching = fresh
end sub

function IndexOf(id as String) as Integer
    if id = "" then return -1
    index = m.indexById[id]
    if index = invalid then return -1
    return index
end function

function EntryById(id as String) as Dynamic
    index = IndexOf(id)
    if index < 0 then return invalid
    return m.entries[index]
end function

' A lane's selection that is not in today's lineup (a game from another day).
function SelectionEntry(lane as String, selection as Object, id as String) as Object
    kind = LineupLaneKind(lane)
    entry = LineupEntry(id, kind, LineupText(selection.name), "", "", "")
    if kind = "digital"
        entry.key = LineupText(selection.number)
        entry.sourceId = "digital"
        entry.channelId = LineupText(selection.id)
        entry.label = "Antenna " + entry.key
    else
        entry.key = LineupShortName(LineupText(selection.source_id))
        entry.sourceId = LineupText(selection.source_id)
        entry.channelId = LineupText(selection.channel_id)
        entry.date = LineupText(selection.date)
    end if
    return entry
end function

' ── Guide ────────────────────────────────────────────────────────

sub OpenGuide()
    m.surfTimer.control = "stop"
    m.surfIndex = -1
    m.bannerTimer.control = "stop"
    m.banner.visible = false
    m.notice.visible = false
    moment = UnixNow()
    m.gEarliest = GuideHalfHour(moment)
    m.gStart = m.gEarliest
    m.gTime = moment
    i = IndexOf(m.currentId)
    if i < 0 then i = 0
    m.gIndex = i
    m.gTop = i - 4
    KeepInView()
    m.under.visible = true
    m.guide.visible = true
    PlaceVideo(true)
    RenderGuide()
    PollNow()
end sub

sub CloseGuide()
    m.guide.visible = false
    m.under.visible = false
    PlaceVideo(false)
    if m.pending <> invalid
        ShowBanner(m.pending.entry, m.pending.text, 0)
    else if m.unplayableSince >= 0
        ShowNotice()
    end if
end sub

' Up / Down: the next channel, wrapping like a channel dial.
sub GuideMove(delta as Integer)
    count = m.entries.Count()
    if count = 0 then return
    m.gIndex = (m.gIndex + delta + count) MOD count
    KeepInView()
    RenderGuide()
end sub

' Rewind / Fast-forward: a page of channels.
sub GuidePage(delta as Integer)
    count = m.entries.Count()
    if count = 0 then return
    i = m.gIndex + delta
    if i < 0 then i = 0
    if i > count - 1 then i = count - 1
    m.gIndex = i
    m.gTop = m.gTop + delta
    KeepInView()
    RenderGuide()
end sub

' Left / Right: the programme before or after, moving the window with it.
sub GuideStep(delta as Integer)
    if m.entries.Count() = 0 then return
    segments = GuideSegments(m.entries[m.gIndex], m.gEarliest, m.gEarliest + m.gridHorizon, UnixNow())
    focus = GuideSegmentAt(segments, m.gTime)
    move = GuideNeighbour(segments, focus, delta, m.gStart, m.gridSpan, m.gEarliest, m.gEarliest + m.gridHorizon - m.gridSpan)
    if move = invalid then return
    m.gStart = move.windowStart
    m.gTime = move.time
    RenderGuide()
end sub

' OK: watch the highlighted channel.
sub GuideSelect()
    if m.entries.Count() = 0 then return
    entry = m.entries[m.gIndex]
    if not entry.enabled
        SetGuideNotice(entry.name + " is not available now")
        return
    end if
    CloseGuide()
    if entry.id = m.currentId and m.pending = invalid
        ShowBanner(m.watching, "", 5)
    else
        Choose(entry)
    end if
end sub

sub KeepInView()
    count = m.entries.Count()
    if m.gIndex < m.gTop then m.gTop = m.gIndex
    if m.gIndex > m.gTop + m.gridRows - 1 then m.gTop = m.gIndex - m.gridRows + 1
    if m.gTop > count - m.gridRows then m.gTop = count - m.gridRows
    if m.gTop < 0 then m.gTop = 0
end sub

sub RenderGuide()
    moment = UnixNow()
    toTime = m.gEarliest + m.gridHorizon
    count = m.entries.Count()
    focusEntry = invalid
    focusSegment = invalid
    for r = 0 to m.gridRows - 1
        i = m.gTop + r
        if i < count
            entry = m.entries[i]
            segments = GuideSegments(entry, m.gEarliest, toTime, moment)
            focus = -1
            if i = m.gIndex
                focus = GuideSegmentAt(segments, m.gTime)
                focusEntry = entry
                focusSegment = segments[focus]
            end if
            m.rows[r].row = {number: entry.key, name: entry.name, focused: i = m.gIndex, tuned: entry.id = m.currentId, enabled: entry.enabled, cells: GuideCells(segments, m.gStart, m.gridSpan, focus, m.gridWidth)}
        else
            m.rows[r].row = {}
        end if
    end for
    m.gDay.text = DayText(m.gStart, false)
    for k = 0 to 3
        m.ticks[k].text = LineupClockAt(m.gStart + k * 1800)
    end for
    offset = moment - m.gStart
    m.gNow.visible = offset >= 0 and offset < m.gridSpan
    if m.gNow.visible then m.gNow.translation = [202 + Int(offset * m.gridWidth / m.gridSpan) - 6, 238]
    RenderInfo(focusEntry, focusSegment, moment)
    RenderPreview()
    UpdateGuideStatus()
end sub

' The panel over the grid: the highlighted programme.
sub RenderInfo(entry as Dynamic, segment as Dynamic, moment as Integer)
    if entry = invalid
        m.infoChan.text = ""
        SetShadowed(m.infoTitle, m.infoTitleShadow, "Loading channels…")
        m.infoTime.text = ""
        m.infoDesc.text = ""
        m.infoBadge.visible = false
        return
    end if
    m.infoChan.text = LineupJoin([LineupJoin([entry.key, entry.name], "  "), entry.label], " · ")
    SetShadowed(m.infoTitle, m.infoTitleShadow, segment.title)
    badge = ""
    if segment.listed
        minutes = (segment.finish - segment.start) \ 60
        m.infoTime.text = DayText(segment.start, true) + " " + LineupClockAt(segment.start) + " - " + LineupClockAt(segment.finish) + "     " + minutes.ToStr() + " min"
        if segment.start <= moment and moment < segment.finish then badge = "ON NOW"
        if segment.live then badge = "LIVE"
    else if not entry.enabled
        m.infoTime.text = "Not available now"
    else
        m.infoTime.text = ""
    end if
    m.infoBadge.visible = badge <> ""
    m.infoBadgeText.text = badge
    description = segment.description
    if description = "" and segment.listed and entry.kind = "digital" then description = "No description — this station sends titles only."
    m.infoDesc.text = description
end sub

' The preview box's caption: what is playing in it.
sub RenderPreview()
    entry = m.watching
    if entry.id = "" and m.pending <> invalid then entry = m.pending.entry
    m.pipBug.text = LineupJoin([entry.key, entry.name], "  ")
    if m.pending <> invalid
        m.pipSlate.text = "TUNING"
    else if m.unplayableSince < 0
        m.pipSlate.text = ""
    end if
end sub

sub SetGuideNotice(text as String)
    m.guideNotice = text
    UpdateGuideStatus()
end sub

sub UpdateGuideStatus()
    if m.pending <> invalid and m.pending.text <> ""
        m.gStatus.text = m.pending.entry.name + ": " + m.pending.text
        m.gStatus.color = "0xFFD23FFF"
    else if m.guideNotice <> ""
        m.gStatus.text = m.guideNotice
        m.gStatus.color = "0xFF8A8AFF"
    else if m.lineupError <> "" and m.keptLast and m.lineupLoaded
        m.gStatus.text = m.lineupError + " · showing the last list"
        m.gStatus.color = "0xFF8A8AFF"
    else if m.lineupError <> ""
        m.gStatus.text = m.lineupError + " · trying again"
        m.gStatus.color = "0xFF8A8AFF"
    else if not m.lineupLoaded
        m.gStatus.text = "Loading channels…"
        m.gStatus.color = "0x7086BDFF"
    else
        m.gStatus.text = m.entries.Count().ToStr() + " channels"
        m.gStatus.color = "0x7086BDFF"
    end if
end sub

' ── Channel up / down ────────────────────────────────────────────

sub Surf(delta as Integer)
    count = m.entries.Count()
    if count < 2 then return
    index = m.surfIndex
    if index < 0 then index = IndexOf(m.currentId)
    if index < 0 then index = IndexOf(m.watching.id)
    if index < 0 then index = 0
    for tries = 1 to count
        index = (index + delta + count) MOD count
        if m.entries[index].enabled then exit for
    end for
    m.surfIndex = index
    ShowBanner(m.entries[index], "", 0)
    m.surfTimer.control = "stop"
    m.surfTimer.control = "start"
end sub

sub OnSurfTimer()
    index = m.surfIndex
    m.surfIndex = -1
    if index < 0 or index >= m.entries.Count() then return
    entry = m.entries[index]
    if entry.id = m.currentId and m.pending = invalid
        ShowBanner(m.watching, "", 4)
    else
        Choose(entry)
    end if
end sub

' ── Banner and clock ─────────────────────────────────────────────

' The Watch page's info banner: the channel, what is on now and later.
sub ShowBanner(entry as Object, status as String, seconds as Integer, alert = false as Boolean)
    if entry.id = "" then return
    FillBanner(entry, status, alert)
    m.banner.visible = not m.guide.visible
    m.bannerTimer.control = "stop"
    if seconds > 0
        m.bannerTimer.duration = seconds
        m.bannerTimer.control = "start"
    end if
    UpdateGuideStatus()
end sub

sub FillBanner(entry as Object, status as String, alert as Boolean)
    m.bannerShows = {id: entry.id, status: status, alert: alert}
    moment = UnixNow()
    SetShadowed(m.bnNum, m.bnNumShadow, entry.key)
    m.bnCall.text = entry.name
    channel = ""
    if entry.number <> invalid then channel = "Channel " + entry.key
    m.bnSub.text = LineupJoin([channel, entry.label], " · ")
    playing = GuideNow(entry, moment)
    title = playing.title
    description = playing.description
    if not playing.listed and not playing.gap
        ' No listings: what it plays all day.
        title = entry.detail
        description = ""
    end if
    SetShadowed(m.bnTitle, m.bnTitleShadow, title)
    m.bnBar.visible = playing.listed
    if playing.listed
        m.bnTime.text = LineupClockAt(playing.start) + " - " + LineupClockAt(playing.finish)
        percent = LineupProgress(playing, moment)
        if percent < 0 then percent = 0
        m.bnFill.width = 420 * percent / 100
    else
        m.bnTime.text = ""
    end if
    m.bnDesc.text = description
    later = GuideLater(entry, moment, 3)
    m.bnLater.visible = later.Count() > 0
    for i = 0 to 2
        row = m.later[i]
        row.group.visible = i < later.Count()
        if i < later.Count()
            row.time.text = LineupClockAt(later[i].start)
            row.title.text = later[i].title
        end if
    end for
    m.bnStatus.text = status
    if alert then m.bnStatus.color = "0xFF8A8AFF" else m.bnStatus.color = "0xFFD23FFF"
end sub

' The banner up when the listings arrive (the app starts on the channel it
' saved, before they are in) shows them without starting over.
sub RefreshBanner()
    shows = m.bannerShows
    if not m.banner.visible or shows = invalid then return
    entry = EntryById(shows.id)
    if entry <> invalid then FillBanner(entry, shows.status, shows.alert)
end sub

' The banner for a pending tune, kept up until it settles.
sub ShowPending(text as String)
    m.pending.text = text
    ShowBanner(m.pending.entry, text, 0)
    if m.guide.visible then RenderPreview()
end sub

sub OnBannerTimer()
    m.banner.visible = false
end sub

' Bold text with the website's 2 px black drop shadow.
sub SetShadowed(label as Object, shadow as Object, text as String)
    label.text = text
    shadow.text = text
end sub

sub OnTick()
    CheckPlayback()
    p = m.pending
    interval = 0
    if p <> invalid and (p.phase = "navigating" or p.phase = "starting")
        interval = 1
    else if m.watchLane <> "" or m.guide.visible
        interval = 4
    end if
    if interval > 0 and Now() - m.lastPoll >= interval then PollNow()
    UpdateClock()
end sub

sub UpdateClock()
    dt = CreateObject("roDateTime")
    dt.ToLocalTime()
    text = LineupClockText(dt)
    if text = m.minuteShown then return
    m.minuteShown = text
    parts = text.Split(" ")
    SetShadowed(m.bnClock, m.bnClockShadow, parts[0])
    SetShadowed(m.gClock, m.gClockShadow, parts[0])
    m.bnAmPm.text = parts[1]
    m.gAmPm.text = parts[1]
    date = dt.GetWeekday() + ", " + MonthName(dt.GetMonth()) + " " + dt.GetDayOfMonth().ToStr()
    m.bnDate.text = date
    m.gDate.text = date
    ' A new minute: the grid moves on with the half hour, and the "now" mark.
    if m.guide <> invalid and m.guide.visible
        earliest = GuideHalfHour(UnixNow())
        if earliest > m.gEarliest
            m.gEarliest = earliest
            if m.gStart < earliest then m.gStart = earliest
            if m.gTime < earliest then m.gTime = earliest
        end if
        RenderGuide()
    end if
end sub

' "Sun, Oct 25" (or "Sun" alone) for Unix seconds, in the Roku's time zone.
function DayText(seconds as Integer, short as Boolean) as String
    dt = CreateObject("roDateTime")
    dt.FromSeconds(seconds)
    dt.ToLocalTime()
    day = Left(dt.GetWeekday(), 3)
    if short then return day
    return day + ", " + MonthName(dt.GetMonth()) + " " + dt.GetDayOfMonth().ToStr()
end function

function MonthName(month as Integer) as String
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return names[month - 1]
end function

' ── Remote ───────────────────────────────────────────────────────

' The OK that chose a dialog button can reach the scene too (the dialog is
' already gone by then): it must not open the guide.
sub NoteChosenOk()
    m.chosenOk = true
    m.chosenClock.Mark()
end sub

function onKeyEvent(key as String, press as Boolean) as Boolean
    if key = "OK" and m.chosenOk
        m.chosenOk = false
        if press and m.chosenClock.TotalMilliseconds() < 500 then return true
    end if
    if not press then return false
    if m.guide.visible
        if key = "OK"
            GuideSelect()
        else if key = "back"
            CloseGuide()
        else if key = "up"
            GuideMove(-1)
        else if key = "down"
            GuideMove(1)
        else if key = "left"
            GuideStep(-1)
        else if key = "right"
            GuideStep(1)
        else if key = "rewind"
            GuidePage(-m.gridRows)
        else if key = "fastforward"
            GuidePage(m.gridRows)
        else
            return false
        end if
        return true
    end if
    if key = "OK"
        OpenGuide()
    else if key = "right" or key = "up"
        Surf(1)
    else if key = "left" or key = "down"
        Surf(-1)
    else if key = "back" or key = "options"
        ' What is on, as the banner says it.
        ShowBanner(m.watching, "", 6)
    else if key = "play"
        if m.watching.id <> ""
            ' Back to the live edge.
            PlayUrl(WatchUrl())
            ShowBanner(m.watching, "Back to live", 4)
        end if
    else if key = "replay"
        ' The channel before this one.
        if m.previous <> invalid
            entry = EntryById(m.previous.id)
            if entry = invalid then entry = m.previous
            Choose(entry)
        end if
    else
        return false
    end if
    return true
end function

' ── TV Thing ─────────────────────────────────────────────────────

sub Request(tag as String, method as String, path as String, body as Dynamic, timeoutMs as Integer)
    m.taskSeq = m.taskSeq + 1
    task = CreateObject("roSGNode", "ApiTask")
    task.id = "api" + m.taskSeq.ToStr()
    task.tag = tag
    task.serial = m.serial
    task.method = method
    task.url = m.top.serverUrl + path
    if body <> invalid then task.body = body
    task.timeoutMs = timeoutMs
    task.observeField("result", "OnApiResult")
    m.tasks[task.id] = task
    task.control = "run"
end sub

sub OnApiResult(event as Object)
    task = event.getRoSGNode()
    m.tasks.Delete(task.id)
    result = event.getData()
    if task.tag = "tune"
        OnTune(task.serial, result)
    else if task.tag = "status"
        OnStatus(result)
    else if task.tag = "probe"
        OnProbe(result)
    end if
end sub

function LaneStatus(name as String) as Dynamic
    if IsPoolTuner(name)
        ' A pool tuner has a lane's selection and stream, its playlist_path
        ' the one this node relays it at.
        if type(m.tuners) <> "roArray" then return invalid
        for each tuner in m.tuners
            if LineupIsObject(tuner) and LineupText(tuner.id) = name then return tuner
        end for
        return invalid
    end if
    if name = "" or not LineupIsObject(m.lanes) then return invalid
    lane = m.lanes[name]
    if not LineupIsObject(lane) then return invalid
    return lane
end function

function IsPoolTuner(name as String) as Boolean
    return Instr(1, name, ":") > 0
end function

' What to follow after a tune's answer: this node's own lane, or the pool
' tuner ("viviserv2:digital") TV Thing put the channel on, or found locked.
function TunedLane(data as Object, lane as String) as String
    tuner = LineupText(data.tuner)
    colon = Instr(1, tuner, ":")
    if colon = 0 then return lane
    own = Mid(tuner, colon + 1)
    if Left(tuner, colon - 1) = m.node and LaneStatus(own) <> invalid then return own
    return tuner
end function

' The lane's playlist as status names it, relative to the server.
function LanePath(name as String) as String
    lane = LaneStatus(name)
    if lane <> invalid and LineupIsObject(lane.stream)
        path = LineupText(lane.stream.playlist_path)
        if Left(path, 1) = "/" then return path
    end if
    return "/stream/" + name + ".m3u8"
end function

function StreamId(stream as Dynamic) as String
    if not LineupIsObject(stream) then return ""
    return LineupText(stream.id)
end function

function StreamReady(lane as Object) as Boolean
    return LineupIsObject(lane.stream) and LineupIsTrue(lane.stream.ready)
end function

function LockOwner(lock as Dynamic) as String
    if LineupIsObject(lock)
        owner = LineupText(lock.owner)
        if owner <> "" then return owner
    end if
    return "Someone"
end function

function NavigationText(operation as Object) as String
    message = LineupText(operation.message)
    if message = "" or message = "Accepted" then return "The Roku is finding it"
    return message
end function

function OperationProblem(operation as Object) as String
    if LineupIsObject(operation.error)
        message = LineupText(operation.error.message)
        if message <> "" then return message
    end if
    message = LineupText(operation.message)
    if message <> "" then return message
    return "The Roku could not find it"
end function

function StreamProblem(stream as Object) as String
    if LineupIsObject(stream.error)
        message = LineupText(stream.error.message)
        if message <> "" then return message
    end if
    return "The stream failed"
end function

function TuneProblem(r as Object) as String
    if r.code = 0
        if r.error <> "" then return "TV Thing did not answer (" + r.error + ")"
        return "TV Thing did not answer"
    end if
    message = ErrorMessage(r.data)
    if message <> "" then return message
    if r.code = 409 then return "Busy right now; try again in a minute"
    return "It would not change (HTTP " + r.code.ToStr() + ")"
end function

' Why a relay (an internet stream, the Sports Guide, VizelVision) answered
' its playlist with an error: TV Thing's own words when it gave them.
function RelayProblem(r as Object) as String
    if r.code = 0 then return "TV Thing did not answer"
    message = ErrorMessage(r.data)
    if message <> "" then return message
    if r.code = 503 then return "Not on the air right now"
    if r.code = 502 then return "Its source is not answering"
    if r.code = 403 then return "It will not play here"
    if r.code = 404 then return "TV Thing no longer has it"
    return "It would not play (HTTP " + r.code.ToStr() + ")"
end function

' {"error": "text"} or {"error": {"code", "message"}}, or "".
function ErrorMessage(data as Dynamic) as String
    if not LineupIsObject(data) then return ""
    if LineupIsObject(data.error) then return LineupText(data.error.message)
    return LineupText(data.error)
end function

' Seconds since the app started.
function Now() as Integer
    return m.clock.TotalSeconds()
end function

' Unix seconds.
function UnixNow() as Integer
    return CreateObject("roDateTime").AsSeconds()
end function

function Elapsed(since as Integer) as String
    seconds = Now() - since
    return (seconds \ 60).ToStr() + ":" + LineupTwo(seconds MOD 60)
end function
