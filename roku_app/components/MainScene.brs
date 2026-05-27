' States:
'   loading  - fetching channel list on startup
'   idle     - channel list shown, no video
'   tuning   - waiting for VLC + HLS readiness
'   playing  - full-screen video, no UI (up/down browses, tunes after 1s)
'   overlay  - video playing behind the channel list

sub init()
    m.serverUrl    = ""
    m.state        = "loading"
    m.channels     = []
    m.curIdx       = -1
    m.pendingIdx   = -1
    m.pretuneState = "idle"
    m.quality      = "medium"
    m.browseIdx    = -1

    m.video       = m.top.findNode("videoPlayer")
    m.panel       = m.top.findNode("channelPanel")
    m.list        = m.top.findNode("channelList")
    m.spinner     = m.top.findNode("spinner")
    m.chCount     = m.top.findNode("channelCount")
    m.tuneOverlay = m.top.findNode("tuningOverlay")
    m.tuningMsg   = m.top.findNode("tuningMsg")
    m.tuningCh    = m.top.findNode("tuningCh")
    m.channelInfo = m.top.findNode("channelInfo")
    m.infoNumber  = m.top.findNode("infoNumber")
    m.infoName    = m.top.findNode("infoName")
    m.infoRF      = m.top.findNode("infoRF")

    ' Debounce timer for channel browsing with up/down
    m.tuneTimer = CreateObject("roSGNode", "Timer")
    m.tuneTimer.duration = 1.0
    m.tuneTimer.repeat = false
    m.tuneTimer.observeField("fire", "OnTuneTimerFire")

    m.video.observeField("state", "OnVideoState")
    m.list.observeField("itemSelected", "OnChannelSelected")
end sub

' ── Server URL set from main.brs ─────────────────────────────────

sub onServerUrl()
    m.serverUrl = m.top.serverUrl
    print "[TV] onServerUrl: " m.serverUrl
    m.panel.visible = true
    if m.serverUrl <> "" then RunChannelLoader()
end sub

' ── Channel loading ──────────────────────────────────────────────

sub RunChannelLoader()
    m.channelTask = CreateObject("roSGNode", "ChannelLoaderTask")
    m.channelTask.serverUrl = m.serverUrl
    m.channelTask.observeField("content", "OnChannelsLoaded")
    m.channelTask.control = "run"
end sub

sub OnChannelsLoaded()
    print "[TV] OnChannelsLoaded fired"
    content = m.channelTask.content
    if content = invalid or content.getChildCount() = 0
        m.chCount.text = "Failed to load channels"
        m.spinner.visible = false
        return
    end if

    m.channels = []
    for i = 0 to content.getChildCount() - 1
        child = content.getChild(i)
        rf = 0
        if child.rfChannel <> invalid then rf = child.rfChannel
        m.channels.push({
            id: child.channelId,
            name: child.title,
            number: child.number,
            rfChannel: rf
        })
    end for

    m.list.content = content
    m.list.visible = true
    m.spinner.visible = false
    m.chCount.text = m.channels.count().toStr() + " channels"

    ' Check if server is already streaming — if so, skip to playing
    m.statusTask = CreateObject("roSGNode", "StatusTask")
    m.statusTask.serverUrl = m.serverUrl
    m.statusTask.observeField("result", "OnStartupStatus")
    m.statusTask.control = "run"
end sub

sub OnStartupStatus()
    status = m.statusTask.result
    if status <> invalid and status.streaming = true and status.channel <> invalid
        ' Find the matching channel index
        chId = status.channel.id
        for i = 0 to m.channels.count() - 1
            if m.channels[i].id = chId
                print "[TV] Already streaming " chId " — jumping to playing"
                m.curIdx    = i
                m.browseIdx = i
                StartVideo()
                SetState("playing")
                return
            end if
        end for
    end if
    print "[TV] Not streaming — showing channel list"
    SetState("idle")
end sub

' ── Channel browsing (debounced up/down in playing state) ────────

sub BrowseChannel(idx as Integer)
    if idx < 0 or idx >= m.channels.count() then return
    m.browseIdx = idx
    ch = m.channels[idx]

    m.infoNumber.text = ch.number
    m.infoName.text   = ch.name
    rfStr = ""
    if ch.rfChannel <> 0 then rfStr = "RF " + ch.rfChannel.toStr()
    m.infoRF.text = rfStr
    m.channelInfo.visible = true

    m.tuneTimer.control = "stop"
    m.tuneTimer.control = "start"
end sub

sub OnTuneTimerFire()
    m.channelInfo.visible = false
    if m.browseIdx >= 0 and m.browseIdx <> m.curIdx
        TuneToIndex(m.browseIdx)
    end if
end sub

' ── Tuning ───────────────────────────────────────────────────────

sub OnChannelSelected()
    TuneToIndex(m.list.itemSelected)
end sub

sub TuneToIndex(idx as Integer)
    if idx < 0 or idx >= m.channels.count() then return
    ch = m.channels[idx]

    m.pendingIdx   = idx
    m.pretuneState = m.state

    m.tuningMsg.text      = "Tuning..."
    m.tuningCh.text       = ch.number + "  " + ch.name
    m.tuneOverlay.visible = true
    m.channelInfo.visible = false

    if m.state = "playing" or m.state = "overlay"
        m.video.control = "stop"
        m.video.content = invalid
        SendStop()
    end if

    m.state = "tuning"

    m.tuneTask = CreateObject("roSGNode", "TuneTask")
    m.tuneTask.serverUrl = m.serverUrl
    m.tuneTask.channelId = ch.id
    m.tuneTask.quality   = m.quality
    m.tuneTask.observeField("result", "OnTuneResult")
    m.tuneTask.control = "run"
end sub

sub OnTuneResult()
    r = m.tuneTask.result
    print "[TV] OnTuneResult: " r
    m.tuneOverlay.visible = false

    if r <> invalid and r.ok = true
        m.curIdx = m.pendingIdx
        StartVideo()
        SetState("playing")
    else
        SetState(m.pretuneState)
        errorMsg = "Tune failed"
        if r <> invalid and r.error <> invalid
            errorMsg = r.error
        end if
        m.chCount.text = errorMsg
    end if
end sub

sub SendStop()
    t = CreateObject("roSGNode", "StopTask")
    t.serverUrl = m.serverUrl
    t.control = "run"
end sub

' ── Video playback ───────────────────────────────────────────────

sub StartVideo()
    content = CreateObject("roSGNode", "ContentNode")
    content.url          = m.serverUrl + "/stream/live.m3u8"
    content.streamFormat = "hls"
    content.live         = true
    m.video.content = content
    m.video.control = "play"
end sub

sub OnVideoState()
    s = m.video.state
    print "[TV] Video state: " s
    if s = "error" or s = "finished"
        m.video.control = "stop"
        m.video.content = invalid
        m.video.visible = false
        m.curIdx = -1
        SetState("idle")
    end if
end sub

' ── State management ─────────────────────────────────────────────

sub SetState(s as String)
    m.state = s
    if s = "idle"
        m.panel.visible       = true
        m.video.visible       = false
        m.tuneOverlay.visible = false
        m.channelInfo.visible = false
        m.list.setFocus(true)
    else if s = "playing"
        m.panel.visible       = false
        m.video.visible       = true
        m.tuneOverlay.visible = false
        m.channelInfo.visible = false
        m.browseIdx           = m.curIdx
        m.video.setFocus(true)
    else if s = "overlay"
        m.panel.visible       = true
        m.video.visible       = true
        m.tuneOverlay.visible = false
        m.channelInfo.visible = false
        m.list.setFocus(true)
        if m.curIdx >= 0 then m.list.jumpToItem = m.curIdx
    end if
end sub

' ── Key handling ─────────────────────────────────────────────────

function onKeyEvent(key as String, press as Boolean) as Boolean
    if not press then return false

    if m.state = "playing"
        if key = "up"
            BrowseChannel(m.browseIdx - 1)
            return true
        else if key = "down"
            BrowseChannel(m.browseIdx + 1)
            return true
        else if key = "back"
            m.tuneTimer.control = "stop"
            m.channelInfo.visible = false
            SetState("overlay")
            return true
        end if

    else if m.state = "overlay"
        if key = "back"
            SetState("playing")
            return true
        end if

    else if m.state = "tuning"
        if key = "back"
            m.panel.visible = false
            return true
        end if

    else if m.state = "idle"
        if key = "back"
            return false
        end if
    end if

    return false
end function
