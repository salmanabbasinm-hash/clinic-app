Attribute VB_Name = "modPrintSystem"
Option Compare Database
Option Explicit
'==========================================================================
' modPrintSystem  -  Urdu font repair + Front/Back (duplex) visit printing
'
' NOTE: This module is 100% ASCII on purpose. Urdu text is built with ChrW()
' through U("hex hex ...") because the VBA editor is ANSI and turns any Urdu
' typed into code into "?????" (that is what happened to the MsgBox texts in
' the old code).
'
' HOW TO USE (once):
'   1. Make a backup copy of the .mdb.
'   2. Alt+F11 > File > Import File... > select modPrintSystem.bas
'   3. Immediate window (Ctrl+G):   FixUrduFonts       (fixes fonts everywhere)
'                                   BuildPrintReport   (creates rptVisitPrint)
'   4. Open form "Daily Treatment" in design view > btnPrint > On Click ->
'      replace the whole old btnPrint_Click with:
'          Private Sub btnPrint_Click()
'              PrintVisitFromForm Me
'          End Sub
'   5. SMS: set SYNC_TOKEN below; in Module1 make ExecuteSMS just call:  QueueSMS PhoneNumber, MessageText
'   6. Optional: BeautifyForms (colours), QueueAutoReminders (follow-up SMS to phone)
'   7. Debug > Compile, save, close. Done.
'==========================================================================

Private Const URDU_FONT As String = "Jameel Noori Nastaleeq"
Private Const RPT_NAME As String = "rptVisitPrint"
Private Const QRY_NAME As String = "qryVisitPrint"
' False (default) = open the usual Print Preview first (old behaviour), True = print straight away
Private Const DIRECT_PRINT As Boolean = False

Public gPrintPatientID As Long
Public gPrintVisitID As Long
Public gPrintDietCategory As String

Public Function U(ByVal codes As String) As String
    Dim a() As String, i As Long
    a = Split(Trim$(codes), " ")
    For i = 0 To UBound(a)
        If Len(a(i)) > 0 Then U = U & ChrW(CLng("&H" & a(i)))
    Next i
End Function

Private Function HasArabic(ByVal s As String) As Boolean
    Dim i As Long, c As Long
    For i = 1 To Len(s)
        c = AscW(Mid$(s, i, 1))
        If c < 0 Then c = c + 65536
        If c >= &H600 And c <= &H6FF Then HasArabic = True: Exit Function
    Next i
End Function

'---------------------------------------------------------------------------
' 1) FONT REPAIR
'    Calibri on Urdu text boxes and the 'Kasheeda' font variants are replaced
'    by Jameel Noori Nastaleeq. English-only labels (Arial) are left alone.
'---------------------------------------------------------------------------
Public Sub FixUrduFonts()
    Dim n As Long
    n = FixFontsIn(acReport) + FixFontsIn(acForm)
    Debug.Print "Controls changed: " & n
    MsgBox "Font repair finished. Controls changed: " & n, vbInformation, "FixUrduFonts"
End Sub

Private Function FixFontsIn(ByVal objType As AcObjectType) As Long
    Dim ao As AccessObject, coll As Object, nm As String
    Dim ctl As Control, fn As String, doIt As Boolean, cnt As Long
    If objType = acReport Then Set coll = CurrentProject.AllReports Else Set coll = CurrentProject.AllForms
    For Each ao In coll
        nm = ao.Name
        If nm <> RPT_NAME Then
            On Error Resume Next
            If objType = acReport Then
                DoCmd.OpenReport nm, acViewDesign, , , acHidden
            Else
                DoCmd.OpenForm nm, acDesign, , , , acHidden
            End If
            If Err.Number = 0 Then
                Dim obj As Object, changed As Long
                changed = 0
                If objType = acReport Then Set obj = Reports(nm) Else Set obj = Forms(nm)
                For Each ctl In obj.Controls
                    Err.Clear
                    Select Case ctl.ControlType
                    Case acLabel, acTextBox, acComboBox, acListBox, acCommandButton
                        fn = ctl.FontName
                        If Err.Number = 0 Then
                            doIt = False
                            If LCase$(Left$(fn, 22)) = "jameel noori nastaleeq" And fn <> URDU_FONT Then
                                doIt = True
                            ElseIf LCase$(fn) = "calibri" Then
                                If ctl.ControlType = acLabel Or ctl.ControlType = acCommandButton Then
                                    doIt = HasArabic(Nz(ctl.Caption, ""))
                                Else
                                    doIt = True
                                End If
                            End If
                            If doIt Then
                                ctl.FontName = URDU_FONT
                                changed = changed + 1
                                Debug.Print nm & " : " & ctl.Name & "  (" & fn & " -> " & URDU_FONT & ")"
                            End If
                        End If
                    End Select
                Next ctl
                If objType = acReport Then
                    DoCmd.Close acReport, nm, acSaveYes
                Else
                    DoCmd.Close acForm, nm, acSaveYes
                End If
                cnt = cnt + changed
            End If
            Err.Clear
            On Error GoTo 0
        End If
    Next ao
    FixFontsIn = cnt
End Function

'---------------------------------------------------------------------------
' 2) DIET TEXT  (used by the back page of rptVisitPrint)
'---------------------------------------------------------------------------
Private Function ChosenCategory() As String
    If Len(Trim$(gPrintDietCategory)) > 0 Then
        ChosenCategory = Trim$(gPrintDietCategory)
    Else
        ChosenCategory = U("0639 0644 0627 062C 0020 0628 0627 0644 063A 0630 0627 0621 0020 0634 0627 06A9 0631 06CC")   ' default: Shakri diet category
    End If
End Function

Private Function VisitMizaj() As String
    VisitMizaj = Trim$(Nz(DLookup("CurrentPulse", "[Daily Treatment]", _
        "PatientID=" & gPrintPatientID & " AND VisitID=" & gPrintVisitID), ""))
End Function

Public Function GetDietText() As String
    Dim mz As String, v As Variant, crit As String
    mz = VisitMizaj()
    If Len(mz) = 0 Then
        GetDietText = U("0627 0633 0020 0648 0632 0679 0020 0645 06CC 06BA 0020 0645 0632 0627 062C 0020 0645 0646 062A 062E 0628 0020 0646 06C1 06CC 06BA 0020 06A9 06CC 0627 0020 06AF 06CC 0627")   ' no mizaj selected for this visit
        Exit Function
    End If
    crit = "Category='" & Replace(ChosenCategory(), "'", "''") & "' AND MizajName='" & _
           Replace(mz, "'", "''") & " " & U("063A 0630 0627 0626 06CC 06BA") & "'"
    v = DLookup("DietDetail", "MasterDiets", crit)
    If IsNull(v) Then
        GetDietText = U("0627 0633 0020 0645 0632 0627 062C 0020 06A9 06D2 0020 0644 06CC 06D2 0020 0645 0646 062A 062E 0628 0020 063A 0630 0627 0626 06CC 0020 0632 0645 0631 06D2 0020 0645 06CC 06BA 0020 06C1 062F 0627 06CC 0627 062A 0020 062F 0631 062C 0020 0646 06C1 06CC 06BA 0020 06C1 06CC 06BA")   ' no diet saved for this mizaj in this category
    Else
        GetDietText = Replace(CStr(v), ChrW(&H200B), "")
    End If
End Function

Public Function GetDietTitle() As String
    Dim nm As String
    nm = Nz(DLookup("PatientName", "[patient history]", "PatientID=" & gPrintPatientID), "")
    GetDietTitle = nm & "  -  " & VisitMizaj() & "  -  " & ChosenCategory()
End Function

'---------------------------------------------------------------------------
' 3) PRINT BUTTON ENTRY POINT
'---------------------------------------------------------------------------
Public Sub PrintVisitFromForm(frm As Form)
    Dim pid As Long, vid As Long
    On Error GoTo EH
    If frm.Dirty Then frm.Dirty = False
    pid = Nz(frm!PatientID, 0)
    If pid = 0 Then
        MsgBox "Please select / save a patient first.", vbExclamation, "Print"
        Exit Sub
    End If
    ' VisitID restarts for every patient (1,2,3...), so ALWAYS use PatientID + VisitID together
    If frm.NewRecord Then
        vid = Nz(DMax("VisitID", "[Daily Treatment]", "PatientID=" & pid), 0)
    Else
        vid = Nz(frm!VisitID, 0)
    End If
    If vid = 0 Then
        MsgBox "This patient has no visit to print.", vbExclamation, "Print"
        Exit Sub
    End If
    gPrintPatientID = pid
    gPrintVisitID = vid
    gPrintDietCategory = Nz(frm!cmbDiet, "")
    PrintVisit pid, vid
    Exit Sub
EH:
    MsgBox "Print error " & Err.Number & ": " & Err.Description, vbCritical, "Print"
End Sub

Public Sub PrintVisit(ByVal pid As Long, ByVal vid As Long)
    Dim ok As Boolean, ao As AccessObject
    gPrintPatientID = pid
    gPrintVisitID = vid
    For Each ao In CurrentProject.AllReports
        If ao.Name = RPT_NAME Then ok = True
    Next ao
    If Not ok Then BuildPrintReport
    On Error Resume Next
    ' make sure the saved printer setup is: A4, portrait, duplex long edge (front + back)
    DoCmd.OpenReport RPT_NAME, acViewDesign, , , acHidden
    With Reports(RPT_NAME).Printer
        .PaperSize = acPRPSA4
        .Orientation = acPRORPortrait
        .Duplex = acPRDPVertical
    End With
    DoCmd.Close acReport, RPT_NAME, acSaveYes
    On Error GoTo 0
    If DIRECT_PRINT Then
        DoCmd.OpenReport RPT_NAME, acViewNormal, , "PatientID=" & pid & " AND VisitID=" & vid
    Else
        DoCmd.OpenReport RPT_NAME, acViewPreview, , "PatientID=" & pid & " AND VisitID=" & vid
    End If
End Sub

'---------------------------------------------------------------------------
' 4) BUILD THE REPORT  (run once: BuildPrintReport)
'    Page 1 (front) = clinic header, patient, visit, symptoms, tests, prescription
'    Page 2 (back)  = diet chart for the visit mizaj + selected diet category
'---------------------------------------------------------------------------
Public Sub BuildPrintReport()
    Dim sql As String, rpt As Report, tmp As String
    Dim y As Long, RW As Long
    Const SEC_HDR As Integer = 5   'acGroupLevel1Header
    RW = 10700

    sql = "SELECT p.PatientID, p.PatientName, p.[Father/HusbandName] AS FatherHusband, p.Gender, p.Age, " & _
          "p.Adress, p.Phone, d.VisitID, d.VisitDate, d.CurrentPulse, d.Symptoms, d.RequiredTests, " & _
          "d.[Special Note] AS SpecialNote, d.GivenDays, d.MedicineEfffects, d.Fees, d.PaidAmount, " & _
          "d.FinalPriscription " & _
          "FROM [patient history] AS p INNER JOIN [Daily Treatment] AS d ON p.PatientID = d.PatientID;"
    EnsureMoneyFields
    On Error Resume Next
    CurrentDb.QueryDefs.Delete QRY_NAME
    DoCmd.DeleteObject acReport, RPT_NAME
    On Error GoTo 0
    CurrentDb.CreateQueryDef QRY_NAME, sql
    CurrentDb.QueryDefs.Refresh

    Set rpt = CreateReport
    tmp = rpt.Name
    rpt.RecordSource = QRY_NAME
    rpt.Width = RW
    CreateGroupLevel tmp, "VisitID", True, False
    On Error Resume Next
    rpt.Section(acPageHeader).Visible = False
    rpt.Section(acPageFooter).Visible = False
    On Error GoTo 0
    rpt.Section(SEC_HDR).Height = 13600
    rpt.Section(SEC_HDR).KeepTogether = True
    rpt.Section(acDetail).Height = 13000
    rpt.Section(acDetail).ForceNewPage = 1          'diet always starts on a NEW page (the back)

    '---- clinic header
    AddLbl tmp, SEC_HDR, "SALMAN HERBAL CLINIC", 0, 0, RW, 520, 22, True, 2, "Arial Black"
    AddLbl tmp, SEC_HDR, U("0637 0628 0020 0648 0627 0644 062C 0631 0627 062D 062A 0020 0646 06CC 0634 0646 0644 0020 06A9 0648 0646 0633 0644 0020 0641 0627 0631 0020 0637 0628 0020 0627 0633 0644 0627 0645 0020 0622 0628 0627 062F"), 0, 540, RW, 380, 14, False, 2, ""
    AddLbl tmp, SEC_HDR, U("0637 0628 0020 0645 0641 0631 062F 0020 0627 0639 0636 0627 0621 0020 0627 0631 0628 0639 06C1 0020 0020 2013 0020 0020 0637 0628 0020 0627 0633 0644 0627 0645 06CC 0020 0639 0646 0627 0635 0631 0020 0627 0631 0628 0639 06C1"), 0, 920, RW, 380, 14, False, 2, ""
    AddHLine tmp, SEC_HDR, 1340, RW

    '---- patient block (right column = label at right, left column = label in the middle)
    y = 1440
    AddPair tmp, SEC_HDR, U("0646 0627 0645 0020 0645 0631 06CC 0636"), "PatientName", U("062A 0627 0631 06CC 062E"), "VisitDate", y
    y = y + 520
    AddPair tmp, SEC_HDR, U("0648 0627 0644 062F 002F 0634 0648 06C1 0631 0020 06A9 0627 0020 0646 0627 0645"), "FatherHusband", U("0645 0631 06CC 0636 0020 0646 0645 0628 0631"), "PatientID", y
    y = y + 520
    AddPair tmp, SEC_HDR, U("0639 0645 0631"), "Age", U("062C 0646 0633"), "Gender", y
    y = y + 520
    AddPair tmp, SEC_HDR, U("0641 0648 0646"), "Phone", U("067E 062A 06C1"), "Adress", y
    y = y + 580
    AddHLine tmp, SEC_HDR, y - 40, RW

    '---- visit block
    AddPair tmp, SEC_HDR, U("0645 0632 0627 062C"), "CurrentPulse", "", "", y
    y = y + 580
    AddBlock tmp, SEC_HDR, U("0639 0644 0627 0645 0627 062A"), "Symptoms", y, 1300, 13: y = y + 380 + 1300 + 80
    AddBlock tmp, SEC_HDR, U("0645 0637 0644 0648 0628 06C1 0020 0679 0633 0679 0633"), "RequiredTests", y, 420, 12: y = y + 380 + 420 + 80
    AddBlock tmp, SEC_HDR, U("0646 0633 062E 06C1"), "FinalPriscription", y, 3300, 14: y = y + 380 + 3300 + 100
    AddPair tmp, SEC_HDR, U("0645 0642 0631 0631 06C1 0020 0627 06CC 0627 0645"), "GivenDays", U("0622 0626 0646 062F 06C1 0020 0645 0639 0627 0626 0646 06C1 0020 06A9 06CC 0020 062A 0627 0631 06CC 062E"), "=IIf(IsNull([GivenDays]),"""",Format([VisitDate]+[GivenDays],""dd/mm/yyyy""))", y
    y = y + 520
    AddPair tmp, SEC_HDR, U("0631 0642 0645"), "Fees", U("0627 062F 0627 0020 0634 062F 06C1 0020 0631 0642 0645"), "PaidAmount", y
    y = y + 520
    AddPair tmp, SEC_HDR, U("0628 0642 0627 06CC 0627 0020 0645 0631 06CC 0636 0020 06A9 06D2 0020 0630 0645 06D2"), "=BalPatient([PatientID],[VisitID])", U("0628 0642 0627 06CC 0627 0020 0645 0639 0627 0644 062C 0020 06A9 06D2 0020 0630 0645 06D2"), "=BalDoctor([PatientID],[VisitID])", y
    y = y + 580
    AddBlock tmp, SEC_HDR, U("062F 0648 0627 0626 06CC 0020 0627 062B 0631 0627 062A"), "MedicineEfffects", y, 500, 12: y = y + 380 + 500 + 60
    AddBlock tmp, SEC_HDR, U("0646 0648 0679"), "SpecialNote", y, 500, 12

    '---- back page: diet
    Dim t As Control
    Set t = AddTxt(tmp, acDetail, "=GetDietTitle()", 0, 0, RW, 600, 16, True, 2, False)
    AddHLine tmp, acDetail, 640, RW
    Set t = AddTxt(tmp, acDetail, "=GetDietText()", 0, 760, RW, 12000, 20, False, 3, True)

    On Error Resume Next
    rpt.Printer.PaperSize = acPRPSA4
    rpt.Printer.Orientation = acPRORPortrait
    rpt.Printer.TopMargin = 567: rpt.Printer.BottomMargin = 567
    rpt.Printer.LeftMargin = 567: rpt.Printer.RightMargin = 567
    rpt.Printer.Duplex = acPRDPVertical
    On Error GoTo 0

    DoCmd.Close acReport, tmp, acSaveYes
    DoCmd.Rename RPT_NAME, acReport, tmp
    MsgBox "Report " & RPT_NAME & " created.", vbInformation, "BuildPrintReport"
End Sub

' Amount columns: Fees = amount ("raqam"), PaidAmount = paid. Balance is automatic and running:
' amount - paid over all visits of the patient up to this visit.
'   > 0  -> patient owes (BalPatient)      < 0 -> doctor owes the patient (BalDoctor)
Public Sub EnsureMoneyFields()
    Dim f As DAO.Field, ok As Boolean
    On Error Resume Next
    ok = False
    For Each f In CurrentDb.TableDefs("Daily Treatment").Fields
        If f.Name = "PaidAmount" Then ok = True
    Next f
    If Not ok Then CurrentDb.Execute "ALTER TABLE [Daily Treatment] ADD COLUMN PaidAmount DOUBLE", dbFailOnError
End Sub

Private Function RunningNet(ByVal pid As Variant, ByVal vid As Variant) As Double
    Dim rs As DAO.Recordset, n As Double
    On Error GoTo done
    Set rs = CurrentDb.OpenRecordset("SELECT Fees, PaidAmount FROM [Daily Treatment] WHERE PatientID=" & CLng(pid) & " AND VisitID<=" & CLng(vid))
    Do While Not rs.EOF
        n = n + Val(Nz(rs!Fees, 0)) - Val(Nz(rs!PaidAmount, 0))
        rs.MoveNext
    Loop
    rs.Close
done:
    RunningNet = n
End Function

Public Function BalPatient(ByVal pid As Variant, ByVal vid As Variant) As String
    Dim n As Double: n = RunningNet(pid, vid)
    If n > 0 Then BalPatient = Format$(n, "#,##0.##")
End Function

Public Function BalDoctor(ByVal pid As Variant, ByVal vid As Variant) As String
    Dim n As Double: n = RunningNet(pid, vid)
    If n < 0 Then BalDoctor = Format$(-n, "#,##0.##")
End Function

Private Sub Style(c As Control, ByVal fs As Integer, ByVal bld As Boolean, ByVal align As Integer, ByVal fnt As String)
    On Error Resume Next
    c.FontName = IIf(Len(fnt) > 0, fnt, URDU_FONT)
    c.FontSize = fs
    c.FontBold = bld
    c.TextAlign = align            '1 left, 2 center, 3 right
    c.ReadingOrder = 2             'right-to-left
End Sub

Private Function AddLbl(ByVal rn As String, ByVal sec As Long, ByVal cap As String, ByVal x As Long, ByVal y As Long, ByVal w As Long, ByVal h As Long, _
                        ByVal fs As Integer, ByVal bld As Boolean, ByVal align As Integer, ByVal fnt As String) As Control
    Dim c As Control
    Set c = CreateReportControl(rn, acLabel, sec, "", "", x, y, w, h)
    c.Caption = cap
    Style c, fs, bld, align, fnt
    Set AddLbl = c
End Function

Private Function AddTxt(ByVal rn As String, ByVal sec As Long, ByVal src As String, ByVal x As Long, ByVal y As Long, ByVal w As Long, ByVal h As Long, _
                        ByVal fs As Integer, ByVal bld As Boolean, ByVal align As Integer, ByVal bordered As Boolean) As Control
    Dim c As Control
    Set c = CreateReportControl(rn, acTextBox, sec, "", "", x, y, w, h)
    c.ControlSource = src
    c.CanGrow = True
    c.CanShrink = False
    If bordered Then c.BorderStyle = 1 Else c.BorderStyle = 0
    Style c, fs, bld, align, ""
    On Error Resume Next
    If c.Controls.Count > 0 Then DeleteReportControl rn, c.Controls(0).Name     'remove auto label
    Set AddTxt = c
End Function

Private Sub AddHLine(ByVal rn As String, ByVal sec As Long, ByVal y As Long, ByVal w As Long)
    Dim c As Control
    Set c = CreateReportControl(rn, acLine, sec, "", "", 0, y, w, 0)
    c.BorderWidth = 2
End Sub

' one row with two label+value pairs (right pair and left pair)
Private Sub AddPair(ByVal rn As String, ByVal sec As Long, ByVal capR As String, ByVal srcR As String, _
                    ByVal capL As String, ByVal srcL As String, ByVal y As Long)
    If Len(srcR) > 0 Then
        AddLbl rn, sec, capR, 9200, y, 1500, 460, 13, True, 3, ""
        AddTxt rn, sec, srcR, 5700, y, 3400, 460, 13, False, 3, True
    End If
    If Len(srcL) > 0 Then
        AddLbl rn, sec, capL, 4200, y, 1400, 460, 13, True, 3, ""
        AddTxt rn, sec, srcL, 300, y, 3800, 460, 13, False, 3, True
    End If
End Sub

' caption line on top, big boxed value below
Private Sub AddBlock(ByVal rn As String, ByVal sec As Long, ByVal cap As String, ByVal src As String, ByVal y As Long, ByVal h As Long, ByVal fs As Integer)
    AddLbl rn, sec, cap, 6700, y, 4000, 360, 13, True, 3, ""
    AddTxt rn, sec, src, 0, y + 380, 10700, h, fs, False, 3, True
End Sub


'---------------------------------------------------------------------------
' 5) SMS  (works with the phone app, Unicode safe)
'    Messages are put in the phone's queue through the Clinic Sync program
'    (clinic_sync.py running on this PC). The phone shows them in the
'    Reminders tab, one tap opens the SMS app with the Urdu text, and the
'    result comes back into tblManualSMSHistory / tblAutoSMSHistory.
'
'    Put the token printed by clinic_sync.py (sync_config.json) here:
'---------------------------------------------------------------------------
Private Const SYNC_URL As String = "http://localhost:8765"
Private Const SYNC_TOKEN As String = "PUT-YOUR-TOKEN-HERE"

Private Function JsonStr(ByVal s As String) As String
    Dim i As Long, c As Long, r As String
    For i = 1 To Len(s)
        c = AscW(Mid$(s, i, 1))
        If c < 0 Then c = c + 65536
        Select Case c
            Case 34: r = r & "\" & Chr$(34)
            Case 92: r = r & "\\"
            Case 10: r = r & "\n"
            Case 13: r = r & "\r"
            Case 32 To 126: r = r & Chr$(c)
            Case Else: r = r & "\u" & Right$("0000" & Hex$(c), 4)
        End Select
    Next i
    JsonStr =  & r & 
End Function

Public Function QueueSMS(ByVal phone As String, ByVal msg As String, Optional ByVal pid As Long = 0, _
                         Optional ByVal vid As Long = 0, Optional ByVal pname As String = "", _
                         Optional ByVal kind As String = "manual") As Boolean
    Dim http As Object, body As String
    On Error GoTo EH
    body = "{""phone"":" & JsonStr(phone) & ",""text"":" & JsonStr(msg) & ",""pid"":" & pid & _
           ",""vid"":" & vid & ",""name"":" & JsonStr(pname) & ",""kind"":" & JsonStr(kind) & "}"
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    http.Open "POST", SYNC_URL & "/api/sms/enqueue", False
    http.setRequestHeader "Content-Type", "application/json"
    http.setRequestHeader "X-Token", SYNC_TOKEN
    http.send body
    QueueSMS = (http.Status = 200)
    Exit Function
EH:
    QueueSMS = False
End Function

' Queue every patient whose next visit is today or tomorrow (uses the existing qryAutoDetection)
Public Sub QueueAutoReminders()
    Dim rs As DAO.Recordset, n As Long, msg As String, nd As Variant, ph As String
    On Error GoTo EH
    Set rs = CurrentDb.OpenRecordset("SELECT * FROM qryAutoDetection;", dbOpenSnapshot)
    Do While Not rs.EOF
        ph = Trim$(Nz(rs!phone, ""))
        nd = rs!NextVisitDate
        If Len(ph) > 0 And Not IsNull(nd) Then
            msg = U("0627 0644 0633 0644 0627 0645 0020 0639 0644 06CC 06A9 0645 0020") & Nz(rs!PatientName, "") & U("0021 0020 0622 067E 0020 06A9 06D2 0020 0622 0626 0646 062F 06C1 0020 0645 0639 0627 0626 0646 06C1 0020 06A9 06CC 0020 062A 0627 0631 06CC 062E 0020") & Format$(nd, "dd/mm/yyyy") & U("0020 06C1 06D2 06D4 0020 0628 0631 0627 0626 06D2 0020 0645 06C1 0631 0628 0627 0646 06CC 0020 062A 0634 0631 06CC 0641 0020 0644 0627 0626 06CC 06BA 06D4 0020 0633 0644 0645 0627 0646 0020 06C1 0631 0628 0644 0020 06A9 0644 06CC 0646 06A9")
            If QueueSMS(ph, msg, Nz(rs!PatientID, 0), Nz(rs!VisitID, 0), Nz(rs!PatientName, ""), "auto") Then n = n + 1
        End If
        rs.MoveNext
    Loop
    rs.Close
    MsgBox n & " reminder(s) sent to the phone queue.", vbInformation, "QueueAutoReminders"
    Exit Sub
EH:
    MsgBox "QueueAutoReminders error " & Err.Number & ": " & Err.Description, vbCritical
End Sub

'---------------------------------------------------------------------------
' 6) STANDARD THEME  (colours, frames, buttons - no control is moved or resized)
'    Run:   ApplyStandardTheme            (all forms)
'           ApplyStandardTheme True       (forms + old reports, light touch)
'    Undo:  restore your backup copy of the file.
'
'---------------------------------------------------------------------------
Private Function cPrimary() As Long
    cPrimary = RGB(15, 107, 90)
End Function
Private Function cPrimaryHover() As Long
    cPrimaryHover = RGB(11, 85, 71)
End Function
Private Function cPrimaryDown() As Long
    cPrimaryDown = RGB(8, 64, 54)
End Function
Private Function cPage() As Long
    cPage = RGB(238, 243, 241)
End Function
Private Function cPanel() As Long
    cPanel = RGB(229, 238, 234)
End Function
Private Function cFrame() As Long
    cFrame = RGB(160, 190, 180)
End Function
Private Function cText() As Long
    cText = RGB(20, 48, 42)
End Function
Private Function cDanger() As Long
    cDanger = RGB(179, 57, 47)
End Function

Public Sub BeautifyForms()
    ApplyStandardTheme False
End Sub

Public Sub ApplyStandardTheme(Optional ByVal includeReports As Boolean = False)
    Dim n As Long
    If MsgBox("This applies one standard look (colours, frames, buttons) to all forms." & vbCrLf & _
              "Positions and sizes stay the same." & vbCrLf & "Did you make a backup copy of the file?", _
              vbYesNo + vbQuestion, "ApplyStandardTheme") <> vbYes Then Exit Sub
    n = ThemeObjects(acForm)
    If includeReports Then n = n + ThemeObjects(acReport)
    MsgBox "Standard theme applied to " & n & " controls.", vbInformation, "ApplyStandardTheme"
End Sub

Private Function ThemeObjects(ByVal objType As AcObjectType) As Long
    Dim ao As AccessObject, coll As Object, nm As String, obj As Object, ctl As Control
    Dim cnt As Long, secName As Long, i As Long, cap As String
    If objType = acReport Then Set coll = CurrentProject.AllReports Else Set coll = CurrentProject.AllForms
    For Each ao In coll
        nm = ao.Name
        If nm <> RPT_NAME Then
            On Error Resume Next
            If objType = acReport Then
                DoCmd.OpenReport nm, acViewDesign, , , acHidden
                Set obj = Reports(nm)
            Else
                DoCmd.OpenForm nm, acDesign, , , , acHidden
                Set obj = Forms(nm)
            End If
            If Err.Number = 0 Then
                If objType = acForm Then
                    obj.Section(acDetail).BackColor = cPage()
                    obj.Section(acFooter).BackColor = cPage()
                    obj.Section(acHeader).BackColor = cPrimary()
                End If
                For Each ctl In obj.Controls
                    Err.Clear
                    Select Case ctl.ControlType
                    Case acCommandButton
                        If objType = acForm Then
                            cap = LCase$(Nz(ctl.Caption, "") & " " & ctl.Name)
                            If InStr(cap, "delete") > 0 Or InStr(cap, "clear") > 0 Or InStr(cap, "cancel") > 0 Then
                                ctl.BackColor = cDanger()
                                ctl.BorderColor = cDanger()
                            Else
                                ctl.BackColor = cPrimary()
                                ctl.BorderColor = cPrimary()
                            End If
                            ctl.ForeColor = RGB(255, 255, 255)
                            ctl.FontBold = True
                            ctl.SpecialEffect = 0
                            ctl.HoverColor = cPrimaryHover()
                            ctl.HoverForeColor = RGB(255, 255, 255)
                            ctl.PressedColor = cPrimaryDown()
                            ctl.PressedForeColor = RGB(255, 255, 255)
                        End If
                    Case acTextBox, acComboBox, acListBox
                        ctl.BackColor = RGB(255, 255, 255)
                        ctl.ForeColor = cText()
                        ctl.BorderStyle = 1
                        ctl.BorderColor = cFrame()
                        ctl.BorderWidth = 0
                        ctl.SpecialEffect = 0
                    Case acLabel
                        ctl.BackStyle = 0
                        If objType = acForm Then
                            If ctl.Section = acHeader Then ctl.ForeColor = RGB(255, 255, 255) Else ctl.ForeColor = cText()
                        Else
                            ctl.ForeColor = cText()
                        End If
                    Case acSubform
                        ctl.BorderStyle = 1
                        ctl.BorderColor = cFrame()
                        ctl.SpecialEffect = 0
                    Case acRectangle, acLine
                        ctl.BorderColor = cFrame()
                    End Select
                    cnt = cnt + 1
                Next ctl
                If objType = acReport Then
                    obj.Section(acPageHeader).BackColor = cPanel()
                    DoCmd.Close acReport, nm, acSaveYes
                Else
                    DoCmd.Close acForm, nm, acSaveYes
                End If
            End If
            Err.Clear
            On Error GoTo 0
        End If
    Next ao
    ThemeObjects = cnt
End Function
