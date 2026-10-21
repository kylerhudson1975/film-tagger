import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

def test_strip_title_quotes():
    s = db.strip_title_quotes
    assert s('"Some Title"') == "Some Title"
    assert s("\u201cCurly\u201d Title") == "Curly Title"
    assert s("The \u00abBig\u00bb Day") == "The Big Day"
    assert s('A "quoted"  word') == "A quoted word"
    assert s("Rock 'n' Roll") == "Rock 'n' Roll"
    assert s("Don\u2019t Stop") == "Don\u2019t Stop"
    assert s("") == "" and s(None) is None

if __name__ == "__main__":
    test_strip_title_quotes(); print("ok")
