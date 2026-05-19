"""
dashboard.py — Deal scoring engine + Streamlit dashboard.

Run with:
    streamlit run dashboard.py
"""

import math
import sqlite3

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
import os

load_dotenv()
DB = os.getenv("DB")

# ── Data loading ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_data() -> pd.DataFrame:
    conn = sqlite3.connect(DB)
    df = pd.read_sql("SELECT * FROM price_history", conn)
    conn.close()
    df["scrape_date"] = pd.to_datetime(df["scrape_date"])
    return df


def compute_scores(df: pd.DataFrame) -> pd.DataFrame:
    # Latest price per item
    latest = (
        df.sort_values("scrape_date")
        .groupby(["external_id", "source"], as_index=False)
        .last()
    )

    # Historical average per item
    avg = (
        df.groupby(["external_id", "source"])["price"]
        .mean()
        .reset_index()
        .rename(columns={"price": "avg_price"})
    )
    scored = latest.merge(avg, on=["external_id", "source"])

    # ── Score components ──────────────────────────────────────────────────────

    # Discount vs historical average (0–50 pts)
    # 25% off → 50 pts, scales linearly
    scored["discount_pct"] = (
        (scored["avg_price"] - scored["price"]) / scored["avg_price"]
    ).clip(lower=0)
    scored["discount_score"] = (scored["discount_pct"] * 200).clip(upper=50).round(1)

    # Popularity via review count, log-scaled (0–30 pts)
    def review_score(r):
        return min(math.log10(max(r, 1) + 1) / math.log10(10_001) * 30, 30)

    scored["review_score"] = scored["review_count"].apply(review_score).round(1)

    # Cross-retailer: cheapest source for the same brand/model (0–20 pts)
    scored["name_key"] = scored["brand_model"].str.lower().str.strip()
    sources_per_name = scored.groupby("name_key")["source"].nunique()
    on_both = sources_per_name[sources_per_name > 1].index
    scored["on_both_retailers"] = scored["name_key"].isin(on_both)
    min_price = scored[scored["on_both_retailers"]].groupby("name_key")["price"].min()
    scored["cross_score"] = 0.0
    mask = scored["on_both_retailers"] & (
        scored["price"] == scored["name_key"].map(min_price)
    )
    scored.loc[mask, "cross_score"] = 20.0

    # Total
    scored["deal_score"] = (
        scored["discount_score"] + scored["review_score"] + scored["cross_score"]
    ).round(1)

    scored["discount_pct_display"] = (scored["discount_pct"] * 100).round(1)
    scored["savings"] = (scored["avg_price"] - scored["price"]).round(2)

    return scored.sort_values("deal_score", ascending=False)


# ── Helpers ───────────────────────────────────────────────────────────────────

def score_bar(score: float) -> str:
    filled = int(score / 10)
    return "█" * filled + "░" * (10 - filled)


def score_color(score: float) -> str:
    if score >= 60:
        return "🟢"
    if score >= 35:
        return "🟡"
    return "🔴"


# ── App ───────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Market Deal Tracker", page_icon="💰", layout="wide")
st.title("💰 Market Deal Tracker")
st.caption("Deal scores combine price discount, review popularity, and cross-retailer comparison.")

df = load_data()
scored = compute_scores(df)

# ── Sidebar filters ───────────────────────────────────────────────────────────

st.sidebar.header("Filters")

categories = sorted(scored["category"].unique())
sources = sorted(scored["source"].unique())

sel_categories = st.sidebar.multiselect("Category", categories, default=categories)
sel_sources = st.sidebar.multiselect("Source", sources, default=sources)
min_score = st.sidebar.slider("Minimum deal score", 0, 100, 0)
min_reviews = st.sidebar.number_input("Minimum reviews", min_value=0, value=0, step=50)

filtered = scored[
    scored["category"].isin(sel_categories)
    & scored["source"].isin(sel_sources)
    & (scored["deal_score"] >= min_score)
    & (scored["review_count"] >= min_reviews)
]

# ── Summary metrics ───────────────────────────────────────────────────────────

col1, col2, col3, col4 = st.columns(4)
col1.metric("Products tracked", len(filtered))
col2.metric("Avg deal score", f"{filtered['deal_score'].mean():.1f}" if len(filtered) else "—")
col3.metric("Best discount", f"{filtered['discount_pct_display'].max():.1f}%" if len(filtered) else "—")
col4.metric("On both retailers", int(filtered["on_both_retailers"].sum()))

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab1, tab2, tab3 = st.tabs(["🏆 Deal Rankings", "📈 Price History", "🔄 Cross-Retailer"])

# ── Tab 1: Deal Rankings ──────────────────────────────────────────────────────

with tab1:
    st.subheader("Top Deals")

    display = filtered[[
        "source", "category", "brand_model", "price",
        "avg_price", "savings", "discount_pct_display",
        "review_count", "deal_score", "discount_score",
        "review_score", "cross_score", "scrape_date",
    ]].copy()
    display.columns = [
        "Source", "Category", "Product", "Price",
        "Avg Price", "Savings", "Discount %",
        "Reviews", "Deal Score", "Discount Pts",
        "Review Pts", "Cross-Retailer Pts", "Last Seen",
    ]
    display["Last Seen"] = display["Last Seen"].dt.date

    # Score bar column
    display.insert(
        0, "Score",
        display["Deal Score"].apply(lambda s: f"{score_color(s)} {s:.0f}  {score_bar(s)}")
    )

    st.dataframe(
        display.reset_index(drop=True),
        use_container_width=True,
        column_config={
            "Price":        st.column_config.NumberColumn(format="$%.2f"),
            "Avg Price":    st.column_config.NumberColumn(format="$%.2f"),
            "Savings":      st.column_config.NumberColumn(format="$%.2f"),
            "Discount %":   st.column_config.NumberColumn(format="%.1f%%"),
            "Deal Score":   st.column_config.ProgressColumn(min_value=0, max_value=100),
            "Discount Pts": st.column_config.ProgressColumn(min_value=0, max_value=50),
            "Review Pts":   st.column_config.ProgressColumn(min_value=0, max_value=30),
            "Cross-Retailer Pts": st.column_config.ProgressColumn(min_value=0, max_value=20),
        },
        hide_index=True,
    )

    # Score distribution chart
    st.subheader("Score Distribution")
    fig = px.histogram(
        filtered, x="deal_score", nbins=20, color="source",
        labels={"deal_score": "Deal Score", "source": "Source"},
        barmode="overlay", opacity=0.75,
    )
    fig.update_layout(margin=dict(t=20))
    st.plotly_chart(fig, use_container_width=True)

# ── Tab 2: Price History ──────────────────────────────────────────────────────

with tab2:
    st.subheader("Price History")

    options = filtered.apply(
        lambda r: f"[{r['source']}] {r['brand_model']} (${r['price']:.2f})", axis=1
    ).tolist()
    product_label = st.selectbox("Select a product", options)

    if product_label:
        idx = options.index(product_label)
        row = filtered.iloc[idx]
        eid = row["external_id"]
        src = row["source"]

        history = df[(df["external_id"] == eid) & (df["source"] == src)].sort_values("scrape_date")

        if len(history) < 2:
            st.info("Only one data point so far — run the scraper again tomorrow to see price movement.")
        else:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=history["scrape_date"], y=history["price"],
                mode="lines+markers", name="Price",
                line=dict(color="#1f77b4", width=2),
                marker=dict(size=8),
            ))
            avg = history["price"].mean()
            fig.add_hline(
                y=avg, line_dash="dash", line_color="orange",
                annotation_text=f"Avg ${avg:.2f}",
                annotation_position="top right",
            )
            fig.update_layout(
                yaxis_title="Price ($)",
                xaxis_title="Date",
                margin=dict(t=20),
                hovermode="x unified",
            )
            st.plotly_chart(fig, use_container_width=True)

        c1, c2, c3 = st.columns(3)
        c1.metric("Current price", f"${row['price']:.2f}")
        c2.metric("Historical avg", f"${row['avg_price']:.2f}")
        c3.metric("Savings vs avg", f"${row['savings']:.2f}", delta=f"{row['discount_pct_display']:.1f}% off")

# ── Tab 3: Cross-Retailer ─────────────────────────────────────────────────────

with tab3:
    st.subheader("Same Product, Both Retailers")
    st.caption("Matched by product name. Cheapest source is highlighted.")

    both = filtered[filtered["on_both_retailers"]].copy()

    if both.empty:
        st.info("No products found on both Amazon and Walmart yet. Scrape more categories to see cross-retailer comparisons.")
    else:
        for name_key, group in both.groupby("name_key"):
            if len(group) < 2:
                continue
            group = group.sort_values("price")
            cheapest = group.iloc[0]
            priciest = group.iloc[-1]
            diff = priciest["price"] - cheapest["price"]

            with st.expander(
                f"**{cheapest['brand_model']}** — save ${diff:.2f} on {cheapest['source']}",
                expanded=False,
            ):
                cols = st.columns(len(group))
                for col, (_, r) in zip(cols, group.iterrows()):
                    is_best = r["source"] == cheapest["source"]
                    col.metric(
                        label=f"{'✅ ' if is_best else ''}{r['source']}",
                        value=f"${r['price']:.2f}",
                        delta=f"${r['price'] - cheapest['price']:.2f}" if not is_best else "Best price",
                        delta_color="inverse" if not is_best else "off",
                    )
                    col.caption(f"Avg: ${r['avg_price']:.2f} | Score: {r['deal_score']:.0f}")
