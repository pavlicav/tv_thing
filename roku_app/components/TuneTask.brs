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

    ' A successful tune response means VLC has started. Return immediately so
    ' the Video node can attach (or stay attached) while the first new segment
    ' is being produced instead of adding a one-second status-poll delay.
    m.top.result = {ok: true}
end sub
