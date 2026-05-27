sub init()
    m.top.functionName = "RunTune"
end sub

sub RunTune()
    serverUrl = m.top.serverUrl

    ' Step 1: POST /api/tune
    xfer = CreateObject("roUrlTransfer")
    xfer.RetainBodyOnError(true)
    xfer.SetUrl(serverUrl + "/api/tune")
    xfer.AddHeader("Content-Type", "application/json")
    body = FormatJSON({channel_id: m.top.channelId, quality: m.top.quality})
    httpCode = xfer.PostFromString(body)
    print "[TV] TuneTask POST /api/tune -> " httpCode.toStr()

    if httpCode < 200 or httpCode >= 300
        m.top.result = {ok: false, error: "Tune failed (HTTP " + httpCode.toStr() + ")"}
        return
    end if

    ' Step 2: Poll /api/status until streaming=true
    statusXfer = CreateObject("roUrlTransfer")
    statusXfer.SetUrl(serverUrl + "/api/status")
    for attempt = 0 to 14
        sleep(1000)
        raw = statusXfer.GetToString()
        status = ParseJSON(raw)
        print "[TV] TuneTask status poll " attempt.toStr() ": streaming=" raw
        if status <> invalid and status.streaming = true
            ' Step 3: Poll /stream/live.m3u8 until it has .ts segments
            ' Fresh roUrlTransfer + cache-busting param each attempt to avoid cached 404
            for segAttempt = 0 to 9
                m3u8Xfer = CreateObject("roUrlTransfer")
                m3u8Xfer.AddHeader("Cache-Control", "no-cache")
                m3u8Xfer.SetUrl(serverUrl + "/stream/live.m3u8?_=" + segAttempt.toStr())
                playlist = m3u8Xfer.GetToString()
                print "[TV] TuneTask m3u8 poll " segAttempt.toStr() ": " playlist.Len().toStr() " bytes"
                if playlist <> invalid and playlist.Instr(0, ".ts") >= 0
                    m.top.result = {ok: true}
                    return
                end if
                sleep(1000)
            end for
            ' streaming=true but no segments after 5s — try anyway
            m.top.result = {ok: true}
            return
        end if
    end for
    m.top.result = {ok: false, error: "Timed out waiting for stream"}
end sub
