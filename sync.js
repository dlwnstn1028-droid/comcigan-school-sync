
const Timetable = require("comcigan-parser");

const SCHOOL = "휘문고등학교";
const GRADE = 2;
const CLASS = 6;

async function main() {
  const parser = new Timetable();
  await parser.init({ cache: 0 });

  const schools = await parser.search("휘문고");
  const school = schools.find(
    s => s.name === SCHOOL && s.region === "서울"
  );

  if (!school) {
    throw new Error("휘문고등학교 검색 실패");
  }

  console.log("학교 검색 성공:", school.name);

  parser.setSchool(school.code);

  const all = await parser.getTimetable();
  const week = all?.[GRADE]?.[CLASS];

  if (!Array.isArray(week) || week.length < 5) {
    throw new Error("2학년 6반 시간표 조회 실패");
  }

  const days = ["월", "화", "수", "목", "금"];

  const lessons = week.flatMap((periods, day) =>
    (periods || [])
      .filter(Boolean)
      .map((lesson, index) => ({
        day: days[day],
        period: index + 1,
        subject: String(lesson.subject || ""),
        teacher: String(lesson.teacher || "")
      }))
  );

  if (!lessons.length) {
    throw new Error("조회된 수업이 없습니다.");
  }

  console.log(JSON.stringify(lessons, null, 2));

  const url = process.env.GAS_WEBAPP_URL;
  const token = process.env.SYNC_TOKEN;

  if (!url || !token) {
    console.log(
      "전송 설정 없음: 조회 테스트만 수행했습니다."
    );
    return;
  }

  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "text/plain;charset=utf-8"
    },
    body: JSON.stringify({
      token,
      school: SCHOOL,
      grade: GRADE,
      classNum: CLASS,
      fetchedAt: new Date().toISOString(),
      lessons
    }),
    redirect: "follow"
  });

  const result = await response.text();

  if (!response.ok || !result.includes('"ok":true')) {
    throw new Error("Apps Script 전송 실패: " +
      result.slice(0, 300));
  }

  console.log("Google Sheets 전송 성공");
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
