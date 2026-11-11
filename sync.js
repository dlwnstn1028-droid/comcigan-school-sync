
async function main() {
  const urls = [
    "https://컴시간학생.kr",
    "https://comcigan.com"
  ];

  for (const url of urls) {
    try {
      const response = await fetch(url, {
        redirect: "follow",
        signal: AbortSignal.timeout(10000)
      });

      const html = await response.text();

      console.log(JSON.stringify({
        url,
        status: response.status,
        finalUrl: response.url,
        contentType: response.headers.get("content-type"),
        htmlLength: html.length,
        hasSchoolKeyword: html.includes("학교")
      }, null, 2));

    } catch (error) {
      console.error(url, error.message);
    }
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
