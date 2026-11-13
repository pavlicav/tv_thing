sub init()
    m.top.functionName = "Load"
end sub

sub Load()
    base = m.top.serverUrl
    date = m.top.date
    inventory = FetchJson([base + "/api/v1/sources"], 8000)[0]
    if not LineupIsObject(inventory) or type(inventory.sources) <> "roArray"
        m.top.result = {entries: [], complete: false, error: "TV Thing did not answer"}
        return
    end if

    rokuSources = []
    ' The guide's listings, as far ahead as the grid scrolls.
    urls = [base + "/api/v1/sources/digital", base + "/api/v1/guide?hours=" + m.top.hours.ToStr()]
    for each source in inventory.sources
        ' Manual Roku is the on-screen remote, not a channel.
        if LineupText(source.lane) = "roku" and LineupText(source.id) <> "roku-manual"
            rokuSources.Push(source)
            query = ""
            if LineupIsTrue(source.dated) then query = "?date=" + date
            urls.Push(base + "/api/v1/sources/" + LineupText(source.id) + query)
        end if
    end for

    docs = FetchJson(urls, 20000)
    problem = ""
    if docs[0] = invalid then problem = "The antenna channels did not load"
    guide = invalid
    if LineupIsObject(docs[1]) then guide = docs[1]["channels"]
    feeds = {}
    for i = 0 to rokuSources.Count() - 1
        feeds[LineupText(rokuSources[i].id)] = ItemsOf(docs[i + 2])
    end for
    now = CreateObject("roDateTime").AsSeconds()
    entries = LineupBuild(ItemsOf(docs[0]), rokuSources, feeds, date, guide, now, inventory.lineup)
    m.top.result = {entries: entries, complete: problem = "", error: problem}
end sub

' A catalog's items, whether it came as {items: [...]} or a bare list.
function ItemsOf(doc as Dynamic) as Object
    if type(doc) = "roArray" then return doc
    ' Bracket access: "items" is also an associative array method.
    if LineupIsObject(doc) and type(doc["items"]) = "roArray" then return doc["items"]
    return []
end function

' GETs every URL at once; each result is the parsed JSON, or invalid.
function FetchJson(urls as Object, timeoutMs as Integer) as Object
    port = CreateObject("roMessagePort")
    pending = {}
    results = []
    for i = 0 to urls.Count() - 1
        results.Push(invalid)
        xfer = CreateObject("roUrlTransfer")
        xfer.SetMessagePort(port)
        xfer.SetUrl(urls[i])
        xfer.AddHeader("Accept", "application/json")
        if xfer.AsyncGetToString() then pending[xfer.GetIdentity().ToStr()] = {xfer: xfer, index: i}
    end for
    clock = CreateObject("roTimespan")
    while pending.Count() > 0
        remaining = timeoutMs - clock.TotalMilliseconds()
        if remaining <= 0 then exit while
        msg = wait(remaining, port)
        if type(msg) = "roUrlEvent"
            key = msg.GetSourceIdentity().ToStr()
            if pending.DoesExist(key)
                index = pending[key].index
                pending.Delete(key)
                if msg.GetResponseCode() = 200 then results[index] = ParseJson(msg.GetString())
            end if
        end if
    end while
    for each key in pending
        pending[key].xfer.AsyncCancel()
    end for
    return results
end function
