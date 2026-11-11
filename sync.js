
const COMCI_BASE = "http://comci.net:4082";
const ENDPOINT = "36179";

// 참고 프로젝트의 코드입니다.
// 휘문고 학교 코드로 확인된 값이 아닙니다.
const TEST_SCHOOL_CODE = "46043";

function koreanDate() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(new Date());

  const p = Object.fromEntries(
    parts.map(x => [x.type, x.value])
  );

  return `${p.year}-${p.month}-${p.day}`;
}

async function request(url) {
  const response = await fetch(url, {
    headers: {
      "Accept": "*/*",
      "Referer": "http://comci.net:4082/st",
      "X-Requested-With": "XMLHttpRequest",
      "User-Agent": "Mozilla/5.0"
    },
    signal: AbortSignal.timeout(15000)
  });

  const body = await response.text();

  return {
    status: response.status,
    body
  };
}

async function main() {
  const date = koreanDate();
  const timestamp = `${date} 12:00:00`;

  console.log("테스트 날짜:", date);

  const initUrl =
    `${COMCI_BASE}/${ENDPOINT}?17384l${TEST_SCHOOL_CODE}`;

  const init = await request(initUrl);

  console.log("학교 확인 요청:", {
    status: init.status,
    length: init.body.length
  });

  const payload =
    `73629_${TEST_SCHOOL_CODE}_${timestamp}_1`;

  const encoded = Buffer.from(payload, "ascii")
    .toString("base64");

  const url = `${COMCI_BASE}/${ENDPOINT}?${encoded}`;

  const result = await request(url);

  console.log("시간표 요청:", {
    status: result.status,
    length: result.body.length,
    preview: result.body.slice(0, 500)
  });

  if (result.status !== 200) {
    throw new Error("컴시간 서버 응답 오류");
  }

  const end = result.body.lastIndexOf("}");
  const jsonText = end >= 0
    ? result.body.slice(0, end + 1)
    : result.body;

  const data = JSON.parse(jsonText);

  console.log("JSON 변환 성공");
  console.log("최상위 필드:", Object.keys(data));
}

main().catch(error => {
  console.error("조회 테스트 실패:", error.message);
  process.exitCode = 1;
});
