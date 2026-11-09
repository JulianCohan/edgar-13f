"""Run with:  python -m unittest discover tests   (or pytest)"""
import unittest

from thirteenf.parse import check, cusip_ok, fix_cusip, parse_filing, parse_text_line

HEADER = """<SEC-HEADER>
ACCESSION NUMBER:\t\t0000000000-08-000001
CONFORMED SUBMISSION TYPE:\t13F-HR
CONFORMED PERIOD OF REPORT:\t20080331
FILED AS OF DATE:\t\t20080507
\t\tCOMPANY CONFORMED NAME:\t\t\tEXAMPLE   CAPITAL LLC
\t\tCENTRAL INDEX KEY:\t\t\t0000123456
</SEC-HEADER>
"""

TEXT_FILING = HEADER + """<DOCUMENT>
<TYPE>13F-HR
<TEXT>
[ ] is a restatement
Form 13F Information Table Entry Total:     6
Form 13F Information Table Value Total:     $4,640
<TABLE>
<S>                            <C>             <C>        <C>       <C>      <C>  <C>   <C>
AT&T INC                       COM             00206R102     3803     99292       SH    SOLE              7716    91576
                                                              702     18341       SH    OTHER                     18241
ISHARES TR                     RUSSELL 2000    464287955       35       520       SH  PUT   SOLE      520
GEORGIA GULF CORP              COM PAR$0.01 NEW373200302 50    19968    SH       DEFINED 1                19968
Excapsa Software, Inc.         common           Not Avail       40      4412    SH       SOLE      4412
APPLE INC                      COM             37833100        10       100       SH    SOLE               100
GRAND TOTALS                                                 4640  12345
</TABLE>
</TEXT>
</DOCUMENT>
"""

XML_FILING = HEADER.replace("20080331", "20240331") + """<DOCUMENT>
<TYPE>13F-HR
<TEXT>
<XML>
<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">
  <formData><coverPage><reportType>13F HOLDINGS REPORT</reportType></coverPage>
  <summaryPage><tableEntryTotal>2</tableEntryTotal><tableValueTotal>786</tableValueTotal></summaryPage></formData>
</edgarSubmission>
</XML>
</TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>INFORMATION TABLE
<TEXT>
<XML>
<ns1:informationTable xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <ns1:infoTable><ns1:nameOfIssuer>ABBVIE INC</ns1:nameOfIssuer><ns1:titleOfClass>COM</ns1:titleOfClass>
    <ns1:cusip>00287Y109</ns1:cusip><ns1:value>225</ns1:value>
    <ns1:shrsOrPrnAmt><ns1:sshPrnamt>1236</ns1:sshPrnamt><ns1:sshPrnamtType>SH</ns1:sshPrnamtType></ns1:shrsOrPrnAmt>
    <ns1:investmentDiscretion>SOLE</ns1:investmentDiscretion>
    <ns1:votingAuthority><ns1:Sole>1000</ns1:Sole><ns1:Shared>0</ns1:Shared><ns1:None>236</ns1:None></ns1:votingAuthority>
  </ns1:infoTable>
  <ns1:infoTable><ns1:nameOfIssuer>VISA INC</ns1:nameOfIssuer><ns1:titleOfClass>CL A</ns1:titleOfClass>
    <ns1:cusip>92826C839</ns1:cusip><ns1:value>561</ns1:value>
    <ns1:shrsOrPrnAmt><ns1:sshPrnamt>2010</ns1:sshPrnamt><ns1:sshPrnamtType>SH</ns1:sshPrnamtType></ns1:shrsOrPrnAmt>
    <ns1:putCall>Call</ns1:putCall><ns1:investmentDiscretion>DFND</ns1:investmentDiscretion>
    <ns1:votingAuthority><ns1:Sole>0</ns1:Sole><ns1:Shared>0</ns1:Shared><ns1:None>2010</ns1:None></ns1:votingAuthority>
  </ns1:infoTable>
</ns1:informationTable>
</XML>
</TEXT>
</DOCUMENT>
"""


class TestParse(unittest.TestCase):
    def test_cusip_check_digit(self):
        for good in ("037833100", "00206R102", "G90073100", "92826C839"):
            self.assertTrue(cusip_ok(good), good)
        for bad in ("037833101", "464287955", "COMMON123"):
            self.assertFalse(cusip_ok(bad), bad)

    def test_text_filing(self):
        filing, rows = parse_filing(TEXT_FILING)
        self.assertEqual(filing["accession"], "0000000000-08-000001")
        self.assertEqual(filing["manager_name"], "EXAMPLE CAPITAL LLC")
        self.assertEqual((filing["cik"], filing["period"], filing["format"]), (123456, "2008-03-31", "text"))
        self.assertEqual(filing["status"], "ok")
        got = [(r["cusip"], r["value"], r["shares"], r["put_call"], r["discretion"]) for r in rows]
        self.assertEqual(got, [
            ("00206R102", 3803, 99292, None, "SOLE"),
            ("00206R102", 702, 18341, None, "OTHER"),     # continuation row, CUSIP left blank
            ("464287955", 35, 520, "Put", "SOLE"),         # option CUSIP fails the check digit
            ("373200302", 50, 19968, None, "DEFINED"),     # CUSIP glued to the class text
            (None, 40, 4412, None, "SOLE"),                # no identifier at all
            ("037833100", 10, 100, None, "SOLE"),          # leading zero dropped
        ])                                                 # GRAND TOTALS row skipped
        self.assertEqual(rows[1]["name_of_issuer"], "AT&T INC")

    def test_text_line_edge_cases(self):
        def parsed(line):
            r = parse_text_line(line, "")
            return r["cusip"], r["value"], r["shares"]
        # a second CUSIP inside the issuer text must not be taken as the CUSIP
        self.assertEqual(parsed("PALM INC SEE CUSIP 69713P107   COMMON STOCK     696642206     1189    61502 SH  SOLE"),
                         ("696642206", 1189, 61502))
        self.assertEqual(parsed("QUESTRON TECHNOLOGY INC   WT IV EX030402    748372117        3      20731 SH  OTHER  1"),
                         ("748372117", 3, 20731))
        # value and shares columns run together; shares reappear as sole + shared votes
        self.assertEqual(parsed("ADC Telecom         COM    000886101      63312190480SH       sole          2149025       41455"),
                         ("000886101", 6331, 2190480))
        # run together and unsplittable: leave both empty rather than store a garbage value
        self.assertEqual(parsed("AMGEN INC   NOTE 0.375% 2/0  031162AQ3   203192202500000 PRN   Defined 01 02    0    0"),
                         ("031162AQ3", None, None))

    def test_cusip_cleanup_and_near_status(self):
        self.assertEqual(fix_cusip("37833100 "), "037833100")
        self.assertEqual(fix_cusip("BFPMB24"), "BFPMB24")       # SEDOL, left alone
        rows = [dict(cusip=str(i), value=10) for i in range(100)]
        self.assertEqual(check(100, 1000, rows), "ok")
        self.assertEqual(check(101, 1010, rows), "near")         # one row short
        self.assertEqual(check(110, 1100, rows), "count_mismatch")
        self.assertEqual(check(100, 1000000, rows), "ok")        # total stated in dollars

    def test_xml_filing(self):
        filing, rows = parse_filing(XML_FILING)
        self.assertEqual((filing["format"], filing["status"], filing["entry_total"]), ("xml", "ok", 2))
        self.assertEqual(filing["report_type"], "13F HOLDINGS REPORT")
        self.assertEqual(rows[0]["vote_sole"], 1000)
        self.assertEqual((rows[1]["cusip"], rows[1]["put_call"], rows[1]["discretion"]), ("92826C839", "Call", "DFND"))


if __name__ == "__main__":
    unittest.main()
