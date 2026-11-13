' Draws one guide row. Its cells are kept and reused as the guide scrolls.
' Colours are static/guide.html's: --stb-text, --stb-mute and --stb-ink.
sub init()
    m.chan = m.top.findNode("chan")
    m.tuned = m.top.findNode("tuned")
    m.num = m.top.findNode("num")
    m.numShadow = m.top.findNode("numShadow")
    m.name = m.top.findNode("name")
    m.cellGroup = m.top.findNode("cells")
    m.pool = []
end sub

sub OnRow()
    row = m.top.row
    if row = invalid or type(row.cells) <> "roArray"
        m.top.visible = false
        return
    end if
    m.top.visible = true
    if row.focused then m.chan.uri = "pkg:/images/chanfocus.9.png" else m.chan.uri = "pkg:/images/chan.9.png"
    m.tuned.visible = row.tuned
    m.num.text = row.number
    m.numShadow.text = row.number
    m.name.text = row.name
    if row.enabled then m.num.color = "0xFFFFFFFF" else m.num.color = "0x7086BDFF"
    cells = row.cells
    for i = 0 to cells.Count() - 1
        if i >= m.pool.Count() then m.pool.Push(NewCell())
        DrawCell(m.pool[i], cells[i], row.enabled)
    end for
    for i = cells.Count() to m.pool.Count() - 1
        m.pool[i].group.visible = false
    end for
end sub

function NewCell() as Object
    group = m.cellGroup.createChild("Group")
    bg = group.createChild("Poster")
    bg.height = 40
    title = group.createChild("Label")
    title.height = 40
    title.vertAlign = "center"
    title.font = BoldFont(14)
    more = group.createChild("Label")
    more.width = 14
    more.height = 40
    more.vertAlign = "center"
    more.text = "▸"
    more.font = BoldFont(11)
    return {group: group, bg: bg, title: title, more: more}
end function

function BoldFont(size as Integer) as Object
    font = CreateObject("roSGNode", "Font")
    font.uri = "pkg:/fonts/DejaVuSans-Bold.ttf"
    font.size = size
    return font
end function

sub DrawCell(c as Object, cell as Object, enabled as Boolean)
    c.group.visible = true
    c.group.translation = [cell.x, 0]
    ' The 2 px line between cells is the grid's frame showing through.
    width = cell.w - 2
    if width < 1 then width = 1
    c.bg.width = width
    faded = cell.gap or not enabled
    if cell.focused
        c.bg.uri = "pkg:/images/gold.9.png"
        ink = "0x14120AFF"
    else if faded
        c.bg.uri = "pkg:/images/gap.9.png"
        ink = "0x7086BDFF"
    else
        c.bg.uri = "pkg:/images/cell.9.png"
        ink = "0xFFFFFFFF"
    end if
    text = cell.title
    ' A programme that began before the window, as the website marks it.
    if cell.before then text = "◂ " + text
    room = width - 16
    if cell.after then room = room - 12
    c.title.visible = room > 14
    c.title.translation = [8, 0]
    c.title.width = room
    c.title.text = text
    c.title.color = ink
    c.more.visible = cell.after and width > 28
    c.more.translation = [width - 16, 0]
    c.more.color = ink
end sub
