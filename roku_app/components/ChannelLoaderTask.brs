sub init()
    m.top.functionName = "GetContent"
end sub

sub GetContent()
    xfer = CreateObject("roUrlTransfer")
    xfer.RetainBodyOnError(true)
    xfer.SetUrl(m.top.serverUrl + "/api/channels")
    raw = xfer.GetToString()
    print "[TV] ChannelLoader got " raw.Len().toStr() " bytes"
    channels = ParseJSON(raw)
    if channels = invalid
        print "[TV] ChannelLoader: ParseJSON failed"
        return
    end if
    print "[TV] ChannelLoader: " channels.count().toStr() " channels"

    root = CreateObject("roSGNode", "ContentNode")
    for each ch in channels
        item = root.createChild("ContentNode")
        item.title = ch.name
        item.id = ch.id
        item.addFields({number: ch.number, channelId: ch.id, rfChannel: ch.rf_channel})
        parts = ch.number.split(".")
        if parts.count() >= 2 and parts[1] <> "1"
            item.addFields({isSubchannel: true})
        end if
    end for
    m.top.content = root
end sub
