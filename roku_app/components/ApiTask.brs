sub init()
    m.top.functionName = "Send"
end sub

sub Send()
    result = {code: 0, data: invalid, error: ""}
    port = CreateObject("roMessagePort")
    xfer = CreateObject("roUrlTransfer")
    xfer.SetMessagePort(port)
    xfer.SetUrl(m.top.url)
    xfer.RetainBodyOnError(true)
    xfer.AddHeader("Accept", "application/json")
    if m.top.method = "POST"
        xfer.AddHeader("Content-Type", "application/json")
        sent = xfer.AsyncPostFromString(FormatJson(m.top.body))
    else
        sent = xfer.AsyncGetToString()
    end if
    if not sent
        result.error = "The request could not be sent"
        m.top.result = result
        return
    end if

    msg = wait(m.top.timeoutMs, port)
    if type(msg) = "roUrlEvent"
        result.code = msg.GetResponseCode()
        text = msg.GetString()
        if text <> invalid and text <> "" then result.data = ParseJson(text)
        if result.code < 0
            result.error = msg.GetFailureReason()
            result.code = 0
        end if
    else
        xfer.AsyncCancel()
        result.error = "TV Thing did not answer"
    end if
    m.top.result = result
end sub
