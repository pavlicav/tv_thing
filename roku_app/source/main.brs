' TV Thing Roku App
' Edit SERVER_URL to match your TV Thing server's LAN address.

sub Main()
    serverUrl = "http://viviserv:8080"

    screen = CreateObject("roSGScreen")
    port = CreateObject("roMessagePort")
    screen.setMessagePort(port)
    scene = screen.CreateScene("MainScene")
    scene.serverUrl = serverUrl
    screen.show()
    while true
        msg = wait(0, port)
        if type(msg) = "roSGScreenEvent"
            if msg.isScreenClosed() then return
        end if
    end while
end sub
