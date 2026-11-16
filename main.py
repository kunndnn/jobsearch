from jobspy import scrape_jobs

# Expose ASGI app for uvicorn (e.g. uvicorn main:app)
try:
    from backend.server import app
except Exception:
    app = None


def main():
    print("Starting JobSpy scrape...")
    jobs = scrape_jobs(
        site_name=["indeed", "linkedin"],
        search_term="Python Developer",
        location="Remote",
        results_wanted=5,
        hours_old=72,
        country_indeed="USA",
        verbose=1,
    )

    print(f"\nSuccessfully scraped {len(jobs)} jobs!\n")
    if not jobs.empty:
        # Display top columns
        columns_to_show = ["site", "title", "company", "location", "date_posted", "job_url"]
        available_cols = [c for c in columns_to_show if c in jobs.columns]
        print(jobs[available_cols].to_string())

        # Save to CSV
        output_file = "scraped_jobs.csv"
        jobs.to_csv(output_file, index=False)
        print(f"\nResults saved to {output_file}")
    else:
        print("No jobs found or boards returned errors:")
        print(jobs.attrs)

if __name__ == "__main__":
    main()
