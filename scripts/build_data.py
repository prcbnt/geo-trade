#!/usr/bin/env python3
"""
Build per-country JSON files for the trade map.

Sources
  Goods    : CEPII BACI, HS92 (1995 onwards), thousands USD, FOB, reconciled
  Services : OECD-WTO BaTIS, BPM6 (2005 onwards), million USD, balanced values

Output (docs/data/)
  meta.json       years, versions, countries (+ EU27), labels, hub notes
  products.json   HS6 code -> English description
  c/<KEY>.json    one file per country (and EU27):
    x[y][p]   goods exports to p, 21 categories (k$)
    m[y][p]   goods imports from p, total (k$)
    d[y][p]   goods dependencies on p: [hs6, value k$, share %]
    s[y][p]   services exports to p: [total, 12 items] (k$)
    sm[y][p]  services imports from p, total (k$)
    sq[y]     partners for which services exports are reported (not modelled)
  EU27 only:
    xin[y][m] goods exports of the rest of the EU to member m (21 categories)
    sin[y][m] services exports of the rest of the EU to member m
  For EU members, partner "EU27" means the rest of the EU.
"""
import argparse, json, os, re, sys, urllib.request, zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import pycountry

REV = "HS92"
DEFAULT_VERSION = "202601"
BACI_PAGE = "https://www.cepii.fr/CEPII/en/bdd_modele/bdd_modele_item.asp?id=37"
BACI_ZIP = "https://www.cepii.fr/DATA_DOWNLOAD/baci/data/BACI_{rev}_V{ver}.zip"
BATIS_PAGE = "https://www.wto.org/english/res_e/statis_e/trade_datasets_e.htm"
BATIS_ZIP = "https://www.wto.org/english/res_e/statis_e/daily_update_e/OECD-WTO_BATIS_data_BPM6-1.zip"

DEP_SHARE = 0.50
DEP_MIN_K = 1000
DEP_TOP = 12

EU = "EU27"
EU_ISO3 = ["AUT", "BEL", "BGR", "HRV", "CYP", "CZE", "DNK", "EST", "FIN", "FRA", "DEU", "GRC", "HUN", "IRL",
           "ITA", "LVA", "LTU", "LUX", "MLT", "NLD", "POL", "PRT", "ROU", "SVK", "SVN", "ESP", "SWE"]

GROUPS = [
    ("Agriculture & pêche", range(1, 15)),
    ("Agroalimentaire, boissons, tabac", range(15, 25)),
    ("Minerais & minéraux", [25, 26]),
    ("Énergie (pétrole, gaz, charbon)", [27]),
    ("Chimie", [28, 29, 31, 32, 33, 34, 35, 36, 37, 38]),
    ("Pharmacie", [30]),
    ("Plastiques & caoutchouc", [39, 40]),
    ("Cuir, bois, papier", range(41, 50)),
    ("Textile & habillement", range(50, 64)),
    ("Chaussures & accessoires", range(64, 68)),
    ("Pierre, céramique, verre", range(68, 71)),
    ("Métaux précieux & pierres", [71]),
    ("Acier & métaux de base", range(72, 84)),
    ("Machines & équipements", [84]),
    ("Électrique & électronique", [85]),
    ("Automobile", [87]),
    ("Ferroviaire & naval", [86, 89]),
    ("Aéronautique & spatial", [88]),
    ("Instruments optiques & médicaux", [90, 91, 92]),
    ("Armes & munitions", [93]),
    ("Autres produits manufacturés", range(94, 100)),
]
NG = len(GROUPS)
CH2G = np.full(100, NG - 1, dtype=np.int8)
for gi, (_, chs) in enumerate(GROUPS):
    for c in chs:
        CH2G[c] = gi

SITEMS = [
    ("SA", "Travail à façon"),
    ("SB", "Maintenance & réparation"),
    ("SC", "Transport"),
    ("SD", "Voyages & tourisme"),
    ("SE", "Construction"),
    ("SF", "Assurance"),
    ("SG", "Services financiers"),
    ("SH", "Propriété intellectuelle (redevances)"),
    ("SI", "Télécoms & informatique"),
    ("SJ", "Autres services aux entreprises"),
    ("SK", "Culture & loisirs"),
    ("SL", "Services publics n.c.a."),
]
SCOLS = ["S"] + [c for c, _ in SITEMS]

HUBS = {
    "NLD": "Port de Rotterdam : une part importante des importations est réexportée vers le reste de l'UE. Les échanges des Pays-Bas avec l'Asie et les Amériques sont surestimés ; préférez la vue UE-27.",
    "BEL": "Port d'Anvers : une part importante des flux est réexportée vers le reste de l'UE. Les échanges bilatéraux de la Belgique sont surestimés ; préférez la vue UE-27.",
    "SGP": "Entrepôt régional (électronique, pétrole raffiné) : forte part de réexportations, valeur ajoutée locale limitée.",
    "HKG": "L'essentiel des exportations sont des réexportations, majoritairement de produits chinois : une partie du commerce de la Chine transite par Hong Kong.",
    "ARE": "Hub de réexportation (Dubaï, Jebel Ali) vers le Moyen-Orient, l'Afrique et l'Asie centrale : origine et destination finales souvent ailleurs.",
    "IRL": "Siège européen de multinationales (numérique, pharmacie) : redevances, services intragroupe et production sous contrat gonflent les flux, sans lien direct avec l'économie irlandaise.",
    "LUX": "Place financière et holdings : flux de services financiers sans rapport avec l'économie domestique.",
    "CHE": "Raffinage et négoce d'or : les métaux précieux gonflent et font fortement varier les flux de biens. Le négoce de matières premières (Genève) n'apparaît pas ici.",
}


def log(*a):
    print(*a, flush=True)


def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 trade-map-builder"})
    return urllib.request.urlopen(req, timeout=timeout)


def download(url, dest):
    if dest.exists() and dest.stat().st_size > 0 and zipfile.is_zipfile(dest):
        log("Using cached", dest)
        return
    log("Downloading", url)
    with fetch(url, 900) as r, open(dest, "wb") as f:
        done = 0
        while True:
            chunk = r.read(8 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if done % (256 << 20) < (8 << 20):
                log(f"  {done >> 20} MB")
    if not zipfile.is_zipfile(dest):
        sys.exit(f"Not a zip file: {url} (check the URL on the source page)")
    log("Downloaded", dest.stat().st_size >> 20, "MB")


def detect_baci_version():
    try:
        html = fetch(BACI_PAGE).read().decode("utf-8", "ignore")
        found = re.findall(r"BACI_%s_V(\d{6}[a-z]?)\.zip" % REV, html) or re.findall(r"(\d{6}[a-z]?)\*? version", html)
        if found:
            return sorted(set(found))[-1]
    except Exception as e:
        log("BACI version detection failed:", e)
    return DEFAULT_VERSION


def detect_batis_url():
    try:
        html = fetch(BATIS_PAGE).read().decode("latin-1", "ignore")
        found = re.findall(r'href="([^"]*OECD-WTO_BATIS_data_BPM6[^"]*\.zip)"', html, re.I)
        if found:
            u = found[0]
            if u.startswith("/"):
                u = "https://www.wto.org" + u
            elif not u.startswith("http"):
                u = "https://www.wto.org/english/res_e/statis_e/" + u.lstrip("./")
            return u
    except Exception as e:
        log("BaTIS URL detection failed:", e)
    return BATIS_ZIP


def find_member(z, pattern):
    for n in z.namelist():
        if re.search(pattern, os.path.basename(n), re.I):
            return n
    return None


def country_table(z):
    cc = pd.read_csv(z.open(find_member(z, r"^country_codes.*\.csv$")), dtype=str, keep_default_na=False)
    name_col = next(c for c in ["country_name", "country_name_full", "country_name_abbreviation"] if c in cc.columns)
    out = {}
    for _, r in cc.iterrows():
        code = int(r["country_code"])
        iso3 = (r.get("country_iso3") or "").strip().upper()
        iso2 = (r.get("country_iso2") or "").strip().upper()
        nm = r[name_col].strip()
        if code == 490:  # "Other Asia, nes" = Taiwan (see BACI FAQ)
            iso3, iso2, nm = "TWN", "TW", "Taiwan"
        ok3 = bool(re.fullmatch(r"[A-Z]{3}", iso3))
        pc = pycountry.countries.get(alpha_3=iso3) if ok3 else None
        out[code] = {"k": iso3 if ok3 else f"C{code}", "n": nm,
                     "i2": iso2 if re.fullmatch(r"[A-Z]{2}", iso2) else "",
                     "num": pc.numeric if pc else None}
    seen = set()
    for code, v in sorted(out.items()):
        if v["k"] in seen:
            v["k"] = f"C{code}"
        seen.add(v["k"])
    return out


def product_table(z):
    pc = pd.read_csv(z.open(find_member(z, r"^product_codes.*\.csv$")), dtype=str, keep_default_na=False)
    code_col = "code" if "code" in pc.columns else pc.columns[0]
    desc_col = "description" if "description" in pc.columns else pc.columns[1]
    return {str(int(c)): d.strip() for c, d in zip(pc[code_col], pc[desc_col]) if c.strip().isdigit()}


class Store:
    def __init__(self):
        self.d = defaultdict(lambda: defaultdict(dict))

    def put(self, key, field, year, partner, value):
        self.d[key][field].setdefault(year, {})[partner] = value

    def add_list(self, key, field, year, partner, item):
        self.d[key][field].setdefault(year, {}).setdefault(partner, []).append(item)


# ---------------------------------------------------------------- goods
def goods(zpath, store, countries):
    z = zipfile.ZipFile(zpath)
    key_of = {c: v["k"] for c, v in countries.items()}
    key_of[-1] = EU
    eu_codes = np.array([c for c, v in countries.items() if v["k"] in EU_ISO3], dtype=np.int32)
    years = sorted((int(m.group(1)), n) for n in z.namelist() if (m := re.search(r"_Y(\d{4})_V", os.path.basename(n))))
    if not years:
        sys.exit("No yearly CSV in BACI zip")
    used = set()

    def emit_x(piv, ys, only_eu=False):
        for (i, j), row in zip(piv.index, piv.to_numpy()):
            if row.sum() <= 0 or (only_eu and i != -1 and j != -1):
                continue
            store.put(key_of.get(i, f"C{i}"), "x", ys, key_of.get(j, f"C{j}"), row.tolist())
            used.update([i, j])

    def emit_m(ser, ys, only_eu=False):
        for (j, i), v in ser.items():
            if v > 0 and (not only_eu or i == -1 or j == -1):
                store.put(key_of.get(j, f"C{j}"), "m", ys, key_of.get(i, f"C{i}"), int(v))

    def emit_d(df, ic, jc, ys, keep=None):
        tot = df.groupby([jc, "k"])["v"].transform("sum")
        sel = df.assign(sh=df["v"] / tot)
        sel = sel[(sel["sh"] >= DEP_SHARE) & (sel["v"] >= DEP_MIN_K)]
        if keep is not None:
            sel = sel[keep(sel)]
        sel = sel.sort_values("v", ascending=False).groupby([jc, ic]).head(DEP_TOP)
        for j, i, k, v, sh in sel[[jc, ic, "k", "v", "sh"]].itertuples(index=False):
            store.add_list(key_of.get(j, f"C{j}"), "d", ys, key_of.get(i, f"C{i}"), [int(k), int(round(v)), int(round(sh * 100))])

    def pivot(df, ic, jc):
        p = df.groupby([ic, jc, "g"])["v"].sum().unstack("g", fill_value=0.0)
        return p.reindex(columns=range(NG), fill_value=0.0).round().astype("int64")

    for year, member in years:
        log(f"Goods {year}")
        df = pd.read_csv(z.open(member), usecols=["i", "j", "k", "v"],
                         dtype={"i": "int32", "j": "int32", "k": "int32", "v": "float64"})
        df = df[df["i"] != df["j"]]
        df["g"] = CH2G[(df["k"].to_numpy() // 10000).clip(0, 99)]
        ys = str(year)

        # 1. country level
        emit_x(pivot(df, "i", "j"), ys)
        emit_m(df.groupby(["j", "i"])["v"].sum().round().astype("int64"), ys)
        emit_d(df, "i", "j", ys)

        # 2. EU27 as one bloc, extra-EU trade only
        ie, je = np.isin(df["i"].to_numpy(), eu_codes), np.isin(df["j"].to_numpy(), eu_codes)
        e2 = df.assign(i2=np.where(ie, -1, df["i"]), j2=np.where(je, -1, df["j"]))
        e2 = e2[e2["i2"] != e2["j2"]]
        emit_x(pivot(e2, "i2", "j2"), ys, only_eu=True)
        emit_m(e2.groupby(["j2", "i2"])["v"].sum().round().astype("int64"), ys, only_eu=True)
        emit_d(e2, "i2", "j2", ys, keep=lambda s: (s["i2"] == -1) | (s["j2"] == -1))

        # 3. member <-> rest of the EU
        intra = df[ie & je]
        if len(intra):
            px = intra.groupby(["i", "g"])["v"].sum().unstack("g", fill_value=0.0).reindex(columns=range(NG), fill_value=0.0).round().astype("int64")
            for i, row in zip(px.index, px.to_numpy()):
                if row.sum() > 0:
                    store.put(key_of[i], "x", ys, EU, row.tolist())
            pm = intra.groupby(["j", "g"])["v"].sum().unstack("g", fill_value=0.0).reindex(columns=range(NG), fill_value=0.0).round().astype("int64")
            for j, row in zip(pm.index, pm.to_numpy()):
                if row.sum() > 0:
                    store.put(key_of[j], "m", ys, EU, int(row.sum()))
                    store.put(EU, "xin", ys, key_of[j], row.tolist())
            tot = df[je].groupby(["j", "k"])["v"].sum()
            rest = intra.groupby(["j", "k"])["v"].sum()
            sh = (rest / tot.reindex(rest.index)).rename("sh")
            dd = pd.concat([rest.rename("v"), sh], axis=1).reset_index()
            dd = dd[(dd["sh"] >= DEP_SHARE) & (dd["v"] >= DEP_MIN_K)].sort_values("v", ascending=False).groupby("j").head(DEP_TOP)
            for j, k, v, s in dd[["j", "k", "v", "sh"]].itertuples(index=False):
                store.add_list(key_of[j], "d", ys, EU, [int(k), int(round(v)), int(round(s * 100))])
        del df, e2, intra
    return [y for y, _ in years], used


# ---------------------------------------------------------------- services
def services(zpath, store, iso2_key):
    z = zipfile.ZipFile(zpath)
    name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
    log("Services: reading", name)
    cols = ["Reporter", "type_Reporter", "Partner", "type_Partner", "Flow", "Item_code", "Year",
            "Final_value_methodology", "Balanced_value"]
    keep_x, keep_q = [], []
    for ch in pd.read_csv(z.open(name), chunksize=4_000_000, usecols=cols,
                          dtype={"Reporter": str, "Partner": str, "Flow": str, "Item_code": str,
                                 "type_Reporter": str, "type_Partner": str, "Final_value_methodology": str},
                          keep_default_na=False, na_values=[""]):
        ch = ch[(ch["type_Reporter"] == "c") & (ch["type_Partner"] == "c")]
        keep_x.append(ch[(ch["Flow"] == "X") & ch["Item_code"].isin(SCOLS)][["Reporter", "Partner", "Item_code", "Year", "Balanced_value", "Final_value_methodology"]])
        keep_q.append(ch[(ch["Flow"] == "M") & (ch["Item_code"] == "S")][["Reporter", "Partner", "Year", "Final_value_methodology"]])
    x = pd.concat(keep_x)
    q = pd.concat(keep_q)
    x["r"] = x["Reporter"].map(iso2_key)
    x["p"] = x["Partner"].map(iso2_key)
    x = x.dropna(subset=["r", "p"])
    x = x[x["r"] != x["p"]]
    x["Balanced_value"] = x["Balanced_value"].fillna(0.0) * 1000.0  # M$ -> k$

    # reported flag for total services: exporter declares X, or importer declares M
    decl = lambda s: s.fillna("").str.match(r"^(R|E1)")
    qx = x[x["Item_code"] == "S"][["r", "p", "Year"]].assign(q=decl(x.loc[x["Item_code"] == "S", "Final_value_methodology"]).to_numpy())
    q["p"] = q["Reporter"].map(iso2_key)  # importer
    q["r"] = q["Partner"].map(iso2_key)   # exporter
    q = q.dropna(subset=["r", "p"]).assign(q=lambda d: decl(d["Final_value_methodology"]))[["r", "p", "Year", "q"]]
    qq = pd.concat([qx, q]).groupby(["r", "p", "Year"])["q"].max()

    t = x.pivot_table(index=["r", "p", "Year"], columns="Item_code", values="Balanced_value", aggfunc="sum", fill_value=0.0)
    t = t.reindex(columns=SCOLS, fill_value=0.0)
    t["q"] = qq.reindex(t.index).fillna(False).astype(float)
    t = t.reset_index()
    log(f"Services: {len(t)} pair-years")

    def emit(df, only_eu=False):
        vals = df[SCOLS].round().astype("int64").to_numpy()
        for (r, p, y, qv), row in zip(df[["r", "p", "Year", "q"]].itertuples(index=False), vals):
            if row[0] <= 0 or (only_eu and r != EU and p != EU):
                continue
            ys = str(int(y))
            store.put(r, "s", ys, p, row.tolist())
            store.put(p, "sm", ys, r, int(row[0]))
            if qv >= 0.5:
                store.d[r]["sq"].setdefault(ys, []).append(p)

    emit(t)

    # EU27 bloc, extra-EU only; quality = value-weighted share of reported flows
    isr, isp = t["r"].isin(EU_ISO3), t["p"].isin(EU_ISO3)
    e = t.assign(r=np.where(isr, EU, t["r"]), p=np.where(isp, EU, t["p"]), qv=t["q"] * t["S"])
    e = e[e["r"] != e["p"]]
    g = e.groupby(["r", "p", "Year"])[SCOLS + ["qv"]].sum().reset_index()
    g["q"] = np.where(g["S"] > 0, g["qv"] / g["S"].where(g["S"] > 0, 1), 0)
    emit(g, only_eu=True)

    # member <-> rest of the EU
    intra = t[isr & isp].assign(qv=lambda d: d["q"] * d["S"])
    ex = intra.groupby(["r", "Year"])[SCOLS + ["qv"]].sum().reset_index()
    for r, y, row, qv in zip(ex["r"], ex["Year"], ex[SCOLS].round().astype("int64").to_numpy(), ex["qv"]):
        if row[0] > 0:
            ys = str(int(y))
            store.put(r, "s", ys, EU, row.tolist())
            if qv / row[0] >= 0.5:
                store.d[r]["sq"].setdefault(ys, []).append(EU)
    im = intra.groupby(["p", "Year"])[SCOLS].sum().reset_index()
    for p, y, row in zip(im["p"], im["Year"], im[SCOLS].round().astype("int64").to_numpy()):
        if row[0] > 0:
            ys = str(int(y))
            store.put(p, "sm", ys, EU, int(row[0]))
            store.put(EU, "sin", ys, p, row.tolist())
    return sorted(int(y) for y in t["Year"].unique())


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="auto", help="BACI version (e.g. 202601) or 'auto'")
    ap.add_argument("--zip", default=None, help="Local BACI zip (skips download)")
    ap.add_argument("--batis-zip", default=None, help="Local BaTIS zip (skips download)")
    ap.add_argument("--no-services", action="store_true")
    ap.add_argument("--out", default="docs/data")
    ap.add_argument("--cache", default="/tmp")
    args = ap.parse_args()

    if args.zip:
        bz = Path(args.zip)
        m = re.search(r"_V(\d{6}[a-z]?)", bz.name)
        version = m.group(1) if m else args.version
    else:
        version = detect_baci_version() if args.version == "auto" else args.version
        bz = Path(args.cache) / f"BACI_{REV}_V{version}.zip"
        download(BACI_ZIP.format(rev=REV, ver=version), bz)
    log("BACI version:", version)

    z = zipfile.ZipFile(bz)
    countries = country_table(z)
    products = product_table(z)
    store = Store()
    gyears, used = goods(bz, store, countries)

    syears, sversion = [], None
    if not args.no_services:
        if args.batis_zip:
            sz = Path(args.batis_zip)
        else:
            sz = Path(args.cache) / "BATIS_BPM6.zip"
            download(detect_batis_url(), sz)
        sname = next(n for n in zipfile.ZipFile(sz).namelist() if n.lower().endswith(".csv"))
        m = re.search(r"BPM6_([A-Za-z]+\d{4})", sname)
        sversion = m.group(1) if m else ""
        iso2_key = {v["i2"]: v["k"] for v in countries.values() if v["i2"] and not re.fullmatch(r"C\d+", v["k"])}
        syears = services(sz, store, iso2_key)

    out = Path(args.out)
    (out / "c").mkdir(parents=True, exist_ok=True)
    for f in (out / "c").glob("*.json"):
        f.unlink()
    for key, d in store.d.items():
        with open(out / "c" / f"{key}.json", "w") as f:
            json.dump(d, f, separators=(",", ":"))

    clist = [c for code, c in sorted(countries.items()) if code in used and c["k"] in store.d]
    clist.insert(0, {"k": EU, "n": "European Union (27)", "fr": "Union européenne (27)", "i2": "", "num": None,
                     "members": [k for k in EU_ISO3 if k in store.d]})
    meta = {
        "source": f"CEPII BACI {REV} v{version}",
        "ssource": f"OCDE-OMC BaTIS BPM6 {sversion}" if syears else None,
        "years": gyears,
        "syears": syears,
        "groups": [g for g, _ in GROUPS],
        "sgroups": [l for _, l in SITEMS],
        "dep": {"share": DEP_SHARE, "min_k": DEP_MIN_K},
        "eu": EU,
        "hubs": HUBS,
        "countries": clist,
    }
    with open(out / "meta.json", "w") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))
    with open(out / "products.json", "w") as f:
        json.dump(products, f, ensure_ascii=False, separators=(",", ":"))
    size = sum(p.stat().st_size for p in out.rglob("*.json")) >> 20
    log(f"Done: {len(clist)} entries, goods {gyears[0]}-{gyears[-1]}, services {syears[:1]}-{syears[-1:]}, {size} MB")


if __name__ == "__main__":
    main()
