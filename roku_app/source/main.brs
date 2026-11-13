' TV Thing Roku app
' A cable box for TV Thing: the website's look, the Watch page's lineup
' (antenna, YouTube TV, games, the Sports Guide, VizelVision and the internet
' streams, by TV Thing channel number). Edit serverUrl if viviserv moves.

sub Main()
    ' TV Thing on the LAN the Rokus share (viviserv's ens4f1).
    serverUrl = "http://10.0.0.98:8080"

    screen = CreateObject("roSGScreen")
    port = CreateObject("roMessagePort")
    screen.setMessagePort(port)
    scene = screen.CreateScene("MainScene")
    screen.show()
    scene.serverUrl = serverUrl
    while true
        msg = wait(0, port)
        if type(msg) = "roSGScreenEvent"
            if msg.isScreenClosed() then return
        end if
    end while
end sub
