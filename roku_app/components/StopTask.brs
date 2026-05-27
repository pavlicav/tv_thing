sub init()
    m.top.functionName = "RunStop"
end sub

sub RunStop()
    xfer = CreateObject("roUrlTransfer")
    xfer.SetUrl(m.top.serverUrl + "/api/stop")
    xfer.PostFromString("")
end sub
