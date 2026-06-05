const { Client } = require('@notionhq/client');
const { GoogleGenerativeAI } = require('@google/generative-ai');
const { YoutubeTranscript } = require('youtube-transcript');
const readline = require('readline');

// Load environment variables from .env file
require('dotenv').config();

const NOTION_API_KEY = process.env.NOTION_API_KEY;
const GEMINI_API_KEY = process.env.GEMINI_API_KEY;
const targetPageId = process.env.NOTION_PAGE_ID;

const notion = new Client({ auth: NOTION_API_KEY });
const genAI = new GoogleGenerativeAI(GEMINI_API_KEY);

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout
});

const askQuestion = (query) => {
  return new Promise(resolve => rl.question(query, resolve));
};

async function runGmaxLogger() {
  try {
    // Sleek minimalist link request
    const url = await askQuestion('Enter YouTube URL for analysis : ');
    
    console.log('\n⏳ Ripping transcript from YouTube...');
    const transcript = await YoutubeTranscript.fetchTranscript(url);
    const textOnly = transcript.map(t => t.text).join(' ').substring(0, 30000); 
    console.log('✅ Transcript loaded into AI memory!');

    let keepRunning = true;
    let isFirstQuery = true;
    let sessionPageId = null;

    const model = genAI.getGenerativeModel({ model: "gemini-2.5-flash" });

    // The Continuous Loop
    while (keepRunning) {
      let menuText = isFirstQuery 
        ? '\n🧠 What do you want to extract?\n1. 📝 Quick Summary\n2. ✂️ Clip Potential\n3. 🎯 Key Takeaways & Action Steps\n👉 Select (1-3) OR type your question directly: '
        : '\n🧠 Is there something else you need to know?\n👉 Select (1-3), type a new question, or type "exit" or "done" to finish: ';

      const choice = await askQuestion(menuText);
      const input = choice.trim().toLowerCase();

      // Kill Switch
      if (input === 'exit' || input === 'done' || input === 'quit' || input === 'no') {
        console.log('\n🎬 Wrapping up! Session closed.');
        keepRunning = false;
        break;
      }

      let aiInstruction = '';
      let blockTitle = '';

      if (input === '1') {
        blockTitle = 'Quick Summary';
        aiInstruction = 'Provide a highly detailed but clean summary of this video. Identify the core value or main problem it solves for the viewer. Format with minimalist bullet points.';
      } else if (input === '2') {
        blockTitle = 'Clip Potential';
        aiInstruction = 'Identify the 3 best hooks or most engaging moments in this video that could be cut into high-performing short-form reels. Explain why each hook works.';
      } else if (input === '3') {
        blockTitle = 'Key Takeaways & Action Steps';
        aiInstruction = 'Extract the most valuable lessons, core concepts, or actionable steps from this video. Structure them logically using minimalist bullet points.';
      } else if (input !== '') {
        blockTitle = `Custom Query: ${choice.trim()}`;
        aiInstruction = `Answer the following specific query in extremely high detail based *only* on the transcript provided. Query: ${choice.trim()}`;
      } else {
        console.log('⚠️ Empty input detected. Skipping...');
        continue;
      }

      console.log('⏳ Analyzing transcript...');
      
      const fullPrompt = `You are a professional video researcher and master note-taker. Structure the response logically and cleanly.
      Instructions: ${aiInstruction}
      Transcript: ${textOnly}`;

      const result = await model.generateContent(fullPrompt);
      const aiAnalysis = result.response.text();

      // Chunking Engine: Slices massive responses into safe 1900-char blocks for Notion
      const chunks = aiAnalysis.match(/[\s\S]{1,1900}/g) || [];
      const textBlocks = chunks.map(chunk => ({
        object: 'block',
        type: 'paragraph',
        paragraph: {
          rich_text: [{ type: 'text', text: { content: chunk } }]
        }
      }));

      // FIRST QUERY RUN: Generate dynamic title and create the page
      if (isFirstQuery) {
        console.log('⏳ Generating dynamic Notion title based on video details...');
        
        const titlePrompt = `Read this transcript and generate a short, professional title (max 6-7 words) for a video breakdown document. Try to identify the main topic, the creator/channel, or core subject. If the user's custom query ("${blockTitle}") is highly specific, factor it in. Output ONLY the raw title text, no quotes, no markdown. Transcript: ${textOnly.substring(0, 6000)}`;
        
        const titleResult = await model.generateContent(titlePrompt);
        const dynamicTitle = titleResult.response.text().replace(/["*]/g, '').trim();

        console.log(`✅ Title Generated: "${dynamicTitle}"`);
        console.log('⏳ Pushing assets to your Notion workspace...');

        const newPage = await notion.pages.create({
          parent: { page_id: targetPageId },
          properties: {
            title: {
              title: [
                { text: { content: dynamicTitle } }
              ]
            }
          },
          children: [
            {
              object: 'block',
              type: 'paragraph',
              paragraph: {
                rich_text: [
                  { type: 'text', text: { content: 'Source: ', link: null } },
                  { type: 'text', text: { content: url, link: { url: url } } }
                ]
              }
            },
            {
              object: 'block',
              type: 'divider',
              divider: {} 
            },
            {
              object: 'block',
              type: 'heading_2',
              heading_2: {
                rich_text: [{ type: 'text', text: { content: blockTitle } }]
              }
            },
            ...textBlocks
          ]
        });
        
        sessionPageId = newPage.id;
        console.log('✅ Base page created and data logged!');
        isFirstQuery = false;
        
      } else {
        // REPEATED RUNS: Append seamlessly under a new divider line
        console.log('⏳ Appending new data to your Notion page...');

        await notion.blocks.children.append({
          block_id: sessionPageId,
          children: [
            {
              object: 'block',
              type: 'divider',
              divider: {} 
            },
            {
              object: 'block',
              type: 'heading_2',
              heading_2: {
                rich_text: [{ type: 'text', text: { content: blockTitle } }]
              }
            },
            ...textBlocks
          ]
        });
        console.log('✅ Follow-up data added to workspace!');
      }
    }

  } catch (error) {
    console.error('\n❌ Error:', error.message);
  } finally {
    rl.close();
  }
}

runGmaxLogger();