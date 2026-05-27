sub init()
    m.top.functionName = "CheckStatus"
end sub

sub CheckStatus()
    xfer = CreateObject("roUrlTransfer")
    xfer.SetUrl(m.top.serverUrl + "/api/status")
    raw = xfer.GetToString()
    status = ParseJSON(raw)
    if status <> invalid
        m.top.result = status
    end if
end sub
