from django import forms

class DumpayForm(forms.Form):
    transaction_id = forms.CharField()
    payer_id = forms.CharField()
