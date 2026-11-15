"""Ten business cards to measure the scanner against.

CRM-SEARCH-SCAN-01 Part 2 asks for "a test set of at least 10 varied
cards with field accuracy before and after". There are zero cards on
file — the decision of 15 Nov was to build against my own samples and
to re-measure on ten real ones after this ships.

So these are drawn, not photographed: each specimen is a ground truth
(what the card SAYS) plus a layout (how it is printed) plus the
conditions it is photographed under. Drawing them is what makes the
measurement honest in both directions —

  * the ground truth is known exactly, so "accuracy" is a count and
    not an impression;
  * the hard cases can be chosen rather than waited for: the card
    photographed upside down, the one with three numbers, the one
    with the address only on the back, the grey one, the one that is
    a PDF, the one whose company is only in the email domain.

WHAT THESE ARE NOT

They are drawn in one typeface at one quality. A real card has
foil, reverse type, a logo that reads as letters, and a photograph
taken at an angle in a lobby. Numbers measured here are a floor for
the deterministic half and say nothing about how a reader copes with
gloss — which is exactly why ten real ones get measured after this
ships.

Every name, company and number here is invented.
"""
from __future__ import annotations

import io

#: (key, ground truth, lines as printed, how it is photographed)
#: `back` is what is printed on the back, when the specimen has one.
SPECIMENS = (
    {
        'key': 'plain',
        'what': 'the ordinary card — one number, one address',
        'truth': {
            'name': 'Rajesh Kumar Sharma', 'first_name': 'Rajesh',
            'last_name': 'Sharma',
            'designation': 'Senior Manager - Logistics',
            'department': 'Logistics',
            'company': 'Zenith Steel Works Limited',
            'email': 'rajesh.sharma@zenithsteel.com',
            'mobile': '+919820011111',
            'website': 'zenithsteel.com',
            'city': 'Mumbai', 'state': 'Maharashtra',
            'country': 'India', 'postal_code': '400069',
        },
        'lines': [
            'ZENITH STEEL WORKS LIMITED',
            'Rajesh Kumar Sharma',
            'Senior Manager - Logistics',
            'M: +91 98200 11111',
            'rajesh.sharma@zenithsteel.com',
            'www.zenithsteel.com',
            '301, Tower B, Andheri East,',
            'Mumbai 400069, Maharashtra, India',
        ],
        'shot': {},
    },
    {
        'key': 'upside_down',
        'what': 'photographed the wrong way up',
        'truth': {
            'name': 'Priya Nair', 'first_name': 'Priya', 'last_name': 'Nair',
            'designation': 'General Manager (Projects)',
            'department': 'Projects',
            'company': 'Konkan Heavy Engineering Private Limited',
            'email': 'priya.nair@konkanheavy.co.in',
            'mobile': '+919940055555',
            'website': 'konkanheavy.co.in',
            'city': 'Chennai', 'state': 'Tamil Nadu',
            'country': 'India', 'postal_code': '600032',
        },
        'lines': [
            'Konkan Heavy Engineering Private Limited',
            'Priya Nair',
            'General Manager (Projects)',
            'Mobile +91 99400 55555',
            'priya.nair@konkanheavy.co.in',
            'Plot 14, SIPCOT Industrial Park,',
            'Chennai 600032, Tamil Nadu',
        ],
        'shot': {'rotate': 180},
    },
    {
        'key': 'skewed',
        'what': 'photographed at an angle on a desk',
        'truth': {
            'name': 'Anand Desai', 'first_name': 'Anand',
            'last_name': 'Desai',
            'designation': 'Head - Supply Chain',
            'department': 'Supply Chain',
            'company': 'Saurashtra Cement Corporation',
            'email': 'anand.desai@saurashtracement.com',
            'mobile': '+919825044444',
            'website': 'saurashtracement.com',
            'city': 'Rajkot', 'state': 'Gujarat', 'country': 'India',
            'postal_code': '360001',
        },
        'lines': [
            'SAURASHTRA CEMENT CORPORATION',
            'Anand Desai',
            'Head - Supply Chain',
            '+91 98250 44444',
            'anand.desai@saurashtracement.com',
            'Kalawad Road, Rajkot 360001, Gujarat',
        ],
        'shot': {'skew': 4.5, 'margin': 90},
    },
    {
        'key': 'three_numbers',
        'what': 'three numbers — mobile, desk and a board line',
        'truth': {
            'name': 'Meera Krishnan', 'first_name': 'Meera',
            'last_name': 'Krishnan',
            'designation': 'Deputy General Manager',
            'company': 'Vindhya Power Systems Limited',
            'email': 'meera.k@vindhyapower.in',
            'mobile': '+919845066666',
            'telephone': '+918022334455',
            'website': 'vindhyapower.in',
            'city': 'Bengaluru', 'state': 'Karnataka',
            'country': 'India', 'postal_code': '560066',
        },
        'lines': [
            'VINDHYA POWER SYSTEMS LIMITED',
            'Meera Krishnan',
            'Deputy General Manager',
            'M +91 98450 66666',
            'D 080 2233 4455',
            'T 080 2233 4400',
            'meera.k@vindhyapower.in',
            'Whitefield, Bengaluru 560066, Karnataka',
        ],
        'shot': {},
    },
    {
        'key': 'back_has_address',
        'what': 'the address is printed on the BACK',
        'truth': {
            'name': 'Sanjay Pillai', 'first_name': 'Sanjay',
            'last_name': 'Pillai',
            'designation': 'Procurement Manager',
            'company': 'Malabar Chemicals Limited',
            'email': 'sanjay.pillai@malabarchem.com',
            'mobile': '+919847077777',
            'website': 'malabarchem.com',
            'city': 'Kochi', 'state': 'Kerala', 'country': 'India',
            'postal_code': '682030',
        },
        'lines': [
            'MALABAR CHEMICALS LIMITED',
            'Sanjay Pillai',
            'Procurement Manager',
            '+91 98470 77777',
            'sanjay.pillai@malabarchem.com',
        ],
        'back': [
            'Registered Office',
            'Door 9, Industrial Estate Road,',
            'Kochi 682030, Kerala, India',
            'www.malabarchem.com',
        ],
        'shot': {},
    },
    {
        'key': 'low_contrast',
        'what': 'grey card, grey light — the office lobby photograph',
        'truth': {
            'name': 'Vikram Rathore', 'first_name': 'Vikram',
            'last_name': 'Rathore',
            'designation': 'Logistics Head',
            'company': 'Aravalli Minerals Private Limited',
            'email': 'vikram@aravallimin.com',
            'mobile': '+919829088888',
            'website': 'aravallimin.com',
            'city': 'Jaipur', 'state': 'Rajasthan', 'country': 'India',
            'postal_code': '302018',
        },
        'lines': [
            'ARAVALLI MINERALS PRIVATE LIMITED',
            'Vikram Rathore',
            'Logistics Head',
            '+91 98290 88888',
            'vikram@aravallimin.com',
            'Malviya Nagar, Jaipur 302018, Rajasthan',
        ],
        'shot': {'contrast': 0.28},
    },
    {
        'key': 'company_only_in_domain',
        'what': 'no company line — it is only in the email domain',
        'truth': {
            'name': 'Farida Shaikh', 'first_name': 'Farida',
            'last_name': 'Shaikh',
            'designation': 'Export Manager',
            'company': 'Deccanports',
            'email': 'farida@deccanports.com',
            'mobile': '+919866099999',
            'website': 'deccanports.com',
            'city': 'Hyderabad', 'state': 'Telangana', 'country': 'India',
            'postal_code': '500081',
        },
        'lines': [
            'Farida Shaikh',
            'Export Manager',
            '+91 98660 99999',
            'farida@deccanports.com',
            'Gachibowli, Hyderabad 500081, Telangana',
        ],
        'shot': {},
    },
    {
        'key': 'overseas',
        'what': 'a card from outside India — the dial code decides',
        'truth': {
            'name': 'Khalid Al Mansoori', 'first_name': 'Khalid',
            'last_name': 'Mansoori',
            'designation': 'Operations Director',
            'company': 'Gulf Heavy Logistics LLC',
            'email': 'khalid@gulfheavy.ae',
            'mobile': '+971501234567',
            'website': 'gulfheavy.ae',
            'city': '', 'state': '',
            'country': 'United Arab Emirates', 'postal_code': '',
        },
        'lines': [
            'GULF HEAVY LOGISTICS LLC',
            'Khalid Al Mansoori',
            'Operations Director',
            '+971 50 123 4567',
            'khalid@gulfheavy.ae',
            'Jebel Ali Free Zone, Dubai, United Arab Emirates',
        ],
        'shot': {},
    },
    {
        'key': 'linkedin',
        'what': 'a LinkedIn profile printed on the card',
        'truth': {
            'name': 'Ritu Banerjee', 'first_name': 'Ritu',
            'last_name': 'Banerjee',
            'designation': 'Manager - Business Development',
            'department': 'Business Development',
            'company': 'Hooghly Marine Services',
            'email': 'ritu.b@hooghlymarine.in',
            'mobile': '+919830022222',
            'website': 'hooghlymarine.in',
            'linkedin_url': 'https://linkedin.com/in/ritu-banerjee-8812',
            'city': 'Kolkata', 'state': 'West Bengal', 'country': 'India',
            'postal_code': '700091',
        },
        'lines': [
            'HOOGHLY MARINE SERVICES',
            'Ritu Banerjee',
            'Manager - Business Development',
            '+91 98300 22222',
            'ritu.b@hooghlymarine.in',
            'linkedin.com/in/ritu-banerjee-8812',
            'Salt Lake Sector V, Kolkata 700091, West Bengal',
        ],
        'shot': {},
    },
    {
        'key': 'pdf',
        'what': 'not a photograph at all — a PDF of the card',
        'truth': {
            'name': 'Harpreet Singh', 'first_name': 'Harpreet',
            'last_name': 'Singh',
            'designation': 'Assistant General Manager',
            'company': 'Punjab Agro Movers Limited',
            'email': 'harpreet.singh@punjabagromovers.com',
            'mobile': '+919815033333',
            'website': 'punjabagromovers.com',
            'city': 'Ludhiana', 'state': 'Punjab', 'country': 'India',
            'postal_code': '141010',
        },
        'lines': [
            'PUNJAB AGRO MOVERS LIMITED',
            'Harpreet Singh',
            'Assistant General Manager',
            '+91 98150 33333',
            'harpreet.singh@punjabagromovers.com',
            'Focal Point, Ludhiana 141010, Punjab',
        ],
        'shot': {'as_pdf': True},
    },
    {
        'key': 'tight_crop',
        'what': 'photographed close, filling the frame, slightly dark',
        'truth': {
            'name': 'Lakshmi Iyer', 'first_name': 'Lakshmi',
            'last_name': 'Iyer',
            'designation': 'Senior Executive - Stores',
            'department': 'Stores',
            'company': 'Coromandel Fabricators',
            'email': 'lakshmi.iyer@coromandelfab.com',
            'mobile': '+919840011111',
            'website': 'coromandelfab.com',
            'city': 'Coimbatore', 'state': 'Tamil Nadu',
            'country': 'India', 'postal_code': '641014',
        },
        'lines': [
            'COROMANDEL FABRICATORS',
            'Lakshmi Iyer',
            'Senior Executive - Stores',
            '+91 98400 11111',
            'lakshmi.iyer@coromandelfab.com',
            'Peelamedu, Coimbatore 641014, Tamil Nadu',
        ],
        'shot': {'margin': 6, 'contrast': 0.55},
    },
)

#: What the accuracy report counts. Everything the spec names that a
#: drawn card can carry.
SCORED_FIELDS = ('name', 'first_name', 'last_name', 'designation',
                 'department', 'company', 'email', 'mobile',
                 'telephone', 'website', 'linkedin_url',
                 'city', 'state', 'country', 'postal_code')


def text_of(specimen, *, side='front'):
    """The card as printed, which is what a perfect reader would see."""
    lines = (specimen.get('back') if side == 'back'
             else specimen.get('lines')) or []
    return '\n'.join(lines)


def both_sides_text(specimen):
    text = text_of(specimen)
    back = text_of(specimen, side='back')
    return (text + '\n' + back).strip() if back else text


def _font(size):
    from PIL import ImageFont

    for name in ('DejaVuSans.ttf', 'Arial.ttf',
                 '/System/Library/Fonts/Supplemental/Arial.ttf',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except Exception:                                 # noqa: BLE001
            continue
    return ImageFont.load_default()


def draw(specimen, *, side='front', width=1000, height=580):
    """The specimen as a picture, photographed the way it says.

    `shot` carries the conditions: a quarter turn, a few degrees of
    skew, how much desk is around it, how washed out the light was.
    These are what the preparation step has to undo.
    """
    from PIL import Image, ImageDraw

    shot = specimen.get('shot') or {}
    lines = (specimen.get('back') if side == 'back'
             else specimen.get('lines')) or []
    card = Image.new('RGB', (width, height), 'white')
    pen = ImageDraw.Draw(card)
    ink = (20, 20, 20)
    y = 60
    for n, line in enumerate(lines):
        size = 46 if n == 0 else (40 if n == 1 else 30)
        pen.text((56, y), line, font=_font(size), fill=ink)
        y += size + 18

    contrast = float(shot.get('contrast') or 0)
    if contrast:
        # A grey card under grey light: the whole range squeezed in.
        from PIL import Image as _I
        grey = _I.new('RGB', card.size, (128, 128, 128))
        card = _I.blend(grey, card, contrast)

    margin = int(shot.get('margin', 40))
    if margin:
        desk = Image.new('RGB', (width + margin * 2, height + margin * 2),
                         (156, 150, 142))
        desk.paste(card, (margin, margin))
        card = desk

    skew = float(shot.get('skew') or 0)
    if skew:
        card = card.rotate(skew, expand=True, fillcolor=(156, 150, 142))

    turn = int(shot.get('rotate') or 0)
    if turn:
        card = card.rotate(turn, expand=True)
    return card


def as_bytes(specimen, *, side='front', fmt=None):
    """The specimen as the file somebody would upload."""
    shot = specimen.get('shot') or {}
    want = fmt or ('pdf' if shot.get('as_pdf') else 'png')
    img = draw(specimen, side=side)
    buf = io.BytesIO()
    if want == 'pdf':
        img.convert('RGB').save(buf, format='PDF', resolution=200.0)
        return buf.getvalue(), 'application/pdf'
    img.save(buf, format='PNG')
    return buf.getvalue(), 'image/png'


def by_key(key):
    for one in SPECIMENS:
        if one['key'] == key:
            return one
    raise KeyError(key)
