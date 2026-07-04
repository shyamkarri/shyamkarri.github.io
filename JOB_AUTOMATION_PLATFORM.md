# 🚀 Job Automation Platform - Complete System Requirements & Enhancement Guide

## Project Overview
A comprehensive job application automation, tracking, and recruiter engagement platform with:
- Automated job scraping from 50+ job boards & company career pages
- One-click auto-apply with form filling
- Session-persistent logging & analytics
- Recruiter CRM with email follow-up automation
- Advanced AI voice assistant (South Indian English accent)
- Private authentication & security
- Enhanced frontend portfolio integration

---

## 🏗️ Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    FRONTEND (Portfolio)                      │
│  - Next.js/React with TailwindCSS                           │
│  - AI Voice Assistant (Enhanced)                             │
│  - Dashboard with Real-time Analytics                       │
│  - Glassmorphism Design                                      │
└────────────────────┬────────────────────────────────────────┘
                     │
        ┌────────────┴────────────┐
        │                         │
┌───────▼──────────┐   ┌──────────▼────────┐
│   FastAPI/Node   │   │  WebSocket Server │
│   Backend        │   │  (Live Logging)   │
│   - Auth         │   │  - Sessions       │
│   - Jobs API     │   │  - Streams        │
│   - Logs API     │   │  - Notifications  │
└────────┬─────────┘   └──────────┬────────┘
         │                        │
┌────────▼────────────────────────▼────────┐
│          PostgreSQL Database              │
│  - Users | Jobs | Applications           │
│  - Recruiters | Logs | Sessions          │
│  - Follow-ups | Scrape History          │
└──────────────────────────────────────────┘
         │
┌────────▼──────────────────────────────────┐
│   Job Scraping Layer (Microservice)       │
│  - Puppeteer/Playwright Workers          │
│  - Rotating Proxies                      │
│  - Site-specific Scrapers                │
└───────────────────────────────────────────┘
```

---

## 📊 Database Schema

### Core Tables

```sql
-- USERS TABLE
CREATE TABLE users (
  id UUID PRIMARY KEY,
  email VARCHAR(255) UNIQUE NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  full_name VARCHAR(255),
  phone VARCHAR(20),
  location VARCHAR(255),
  resume_url TEXT,
  linkedin_url TEXT,
  github_url TEXT,
  work_auth_status ENUM('US_CITIZEN', 'GREEN_CARD', 'CPT', 'OPT', 'SPONSORSHIP_OK', 'UNKNOWN'),
  preferred_roles JSON, -- ["SWE", "Data Engineer", "ML Engineer"]
  target_companies JSON, -- Company whitelist/blacklist
  salary_expectations JSON, -- {min: 150000, max: 250000, currency: "USD"}
  created_at TIMESTAMP,
  updated_at TIMESTAMP,
  is_active BOOLEAN DEFAULT true
);

-- JOBS TABLE
CREATE TABLE jobs (
  id UUID PRIMARY KEY,
  job_id_external VARCHAR(255) UNIQUE, -- Platform-specific ID
  source ENUM('LINKEDIN', 'GREENHOUSE', 'WORKDAY', 'LEVER', 'ASHBY', 'COMPANY_CAREER', 'INDEED', 'DICE', 'ZIPRECRUITER'),
  company_name VARCHAR(255) NOT NULL,
  job_title VARCHAR(255) NOT NULL,
  job_url TEXT NOT NULL,
  location VARCHAR(255),
  salary_min INT,
  salary_max INT,
  job_description TEXT,
  required_skills JSON,
  nice_to_have_skills JSON,
  experience_level ENUM('ENTRY', 'MID', 'SENIOR', 'LEAD', 'PRINCIPAL'),
  job_type ENUM('FULL_TIME', 'CONTRACT', 'INTERNSHIP', 'PART_TIME'),
  remote_status ENUM('FULLY_REMOTE', 'HYBRID', 'ON_SITE', 'RELOCATE'),
  applications_count INT DEFAULT 0,
  posted_date TIMESTAMP,
  scraped_at TIMESTAMP,
  is_active BOOLEAN DEFAULT true,
  created_at TIMESTAMP,
  updated_at TIMESTAMP,
  INDEX (company_name, job_title),
  INDEX (source, created_at),
  INDEX (is_active)
);

-- APPLICATIONS TABLE
CREATE TABLE applications (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL,
  job_id UUID NOT NULL,
  application_status ENUM('DRAFT', 'APPLIED', 'VIEWED', 'SHORTLISTED', 'INTERVIEW', 'OFFER', 'REJECTED', 'WITHDRAWN'),
  applied_at TIMESTAMP,
  applied_method ENUM('DIRECT_APPLY', 'AUTO_APPLY', 'MANUAL'),
  cover_letter_generated BOOLEAN,
  cover_letter TEXT,
  job_description_at_apply TEXT, -- Snapshot
  form_data_submitted JSON, -- All form fields filled
  submission_confirmation TEXT, -- Confirmation message/URL
  auto_apply_success BOOLEAN,
  auto_apply_error TEXT,
  created_at TIMESTAMP,
  updated_at TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES users(id),
  FOREIGN KEY (job_id) REFERENCES jobs(id),
  INDEX (user_id, application_status),
  INDEX (applied_at)
);

-- RECRUITERS TABLE
CREATE TABLE recruiters (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL,
  recruiter_name VARCHAR(255),
  recruiter_email VARCHAR(255),
  recruiter_phone VARCHAR(20),
  company_name VARCHAR(255),
  job_id UUID,
  source ENUM('LINKEDIN', 'EMAIL', 'PHONE_CALL', 'EXTRACTED_FROM_JOB'),
  first_contact_date TIMESTAMP,
  last_contact_date TIMESTAMP,
  contact_count INT DEFAULT 0,
  relationship_score INT DEFAULT 0, -- 1-10 scale
  notes TEXT,
  sentiment ENUM('POSITIVE', 'NEUTRAL', 'NEGATIVE'),
  next_followup_date TIMESTAMP,
  is_important BOOLEAN DEFAULT false,
  created_at TIMESTAMP,
  updated_at TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES users(id),
  FOREIGN KEY (job_id) REFERENCES jobs(id),
  UNIQUE(user_id, recruiter_email, company_name),
  INDEX (user_id, last_contact_date)
);

-- SESSION LOGS TABLE (PERSISTENT STREAMING)
CREATE TABLE session_logs (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id UUID NOT NULL,
  session_id VARCHAR(255) NOT NULL,
  log_type ENUM('VOICE_INPUT', 'VOICE_OUTPUT', 'TEXT_INPUT', 'TEXT_OUTPUT', 'JOB_SCRAPED', 'JOB_APPLIED', 'EMAIL_SENT', 'RECRUITER_CONTACTED', 'ERROR', 'SYSTEM'),
  log_level ENUM('INFO', 'WARNING', 'ERROR', 'DEBUG'),
  message TEXT NOT NULL,
  metadata JSON, -- Additional context
  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  created_at TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES users(id),
  INDEX (user_id, session_id, timestamp),
  INDEX (session_id),
  INDEX (created_at)
);

-- EMAIL FOLLOW-UP TEMPLATES
CREATE TABLE email_templates (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL,
  template_name VARCHAR(255),
  template_type ENUM('INITIAL_FOLLOW_UP', 'SECOND_FOLLOW_UP', 'OFFER_NEGOTIATION', 'REJECTION_RESPONSE'),
  subject_template TEXT,
  body_template TEXT,
  variables JSON, -- ["recruiter_name", "company_name", "job_title"]
  is_default BOOLEAN DEFAULT false,
  created_at TIMESTAMP,
  updated_at TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

-- SENT EMAILS TRACKING
CREATE TABLE sent_emails (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL,
  recruiter_id UUID,
  application_id UUID,
  recipient_email VARCHAR(255),
  subject TEXT,
  body TEXT,
  sent_at TIMESTAMP,
  opened BOOLEAN DEFAULT false,
  opened_at TIMESTAMP,
  replied BOOLEAN DEFAULT false,
  replied_at TIMESTAMP,
  reply_text TEXT,
  sent_via ENUM('PLATFORM_EMAIL', 'GMAIL_API', 'SMTP'),
  created_at TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES users(id),
  FOREIGN KEY (recruiter_id) REFERENCES recruiters(id),
  FOREIGN KEY (application_id) REFERENCES applications(id),
  INDEX (user_id, sent_at)
);
```

---

## 🔐 Authentication & Security

### Implementation Required
```
1. JWT-based Authentication
   - Access Token (15 min expiry)
   - Refresh Token (7 day expiry)
   - Session persistence in Redis

2. Password Security
   - bcrypt hashing (salt rounds: 12)
   - Password strength validation (min 12 chars, mixed case, numbers, symbols)

3. OAuth2 Integrations
   - Google Login (for job board auth)
   - LinkedIn Login (for recruiter data extraction)
   - GitHub Login (for portfolio link)

4. Email Verification
   - Verification token (6-digit code)
   - Resend after 5 minutes
   - 24-hour expiry

5. Data Encryption
   - Encrypt sensitive fields: phone, SSN, salary info
   - TLS 1.3 for all connections
   - Environment variable encryption
```

---

## 🤖 AI Voice Assistant Enhancements

### Current Issues & Solutions
```
ISSUE 1: Voice Quality & Accent
  Current: Generic Google/Microsoft voices
  Solution:
    - Use Google Cloud Text-to-Speech API:
      * Language: en-IN (English - India)
      * Voice: "en-IN-Neural2-B" (Male voice, natural)
      * Speaking rate: 0.95
      * Pitch: 0 (neutral male pitch for 28yr old)
    
    - Fallback: ElevenLabs API
      * Custom voice clone (record 10 min sample)
      * South Indian English accent
      * Natural conversational pace

ISSUE 2: Grammar & Speech Quality
  Current: AI might have grammatical errors
  Solution:
    - Add prompt engineering layer:
      ```
      System Prompt:
      "You are Prasad's AI assistant. Speak in conversational South Indian English. 
      - Use simple, clear grammar
      - Avoid complex sentences
      - Use 'is' instead of 'are' for plural nouns (South Indian pattern)
      - Replace 'th' sounds naturally (this → 'dis', that → 'dat' - optional, 
        but can use standard English)
      - Keep response length: 1-3 sentences
      - Tone: Professional but friendly, like talking to a peer
      - Address user as 'brother' or by name"
      ```

ISSUE 3: Context Awareness
  Current: Each message treated independently
  Solution:
    - Maintain conversation context (last 5 exchanges)
    - Remember user profile (role, experience, preferences)
    - Personalize responses based on job applied, recruiter feedback

ISSUE 4: Real-time Streaming
  Current: Audio plays after full response
  Solution:
    - Implement streaming TTS (Google Cloud Speech-to-Text streaming)
    - Buffer audio chunks and play progressively
    - Lip-sync animation with audio (optional)
```

### Voice Configuration
```javascript
// tts-config.js
export const TTS_CONFIG = {
  provider: 'google-cloud', // or 'elevenlabs'
  googleCloud: {
    apiKey: process.env.GOOGLE_CLOUD_API_KEY,
    languageCode: 'en-IN',
    voiceName: 'en-IN-Neural2-B', // Male, South Indian accent
    audioEncoding: 'MP3',
    speakingRate: 0.95,
    pitch: 0,
  },
  elevenLabs: {
    apiKey: process.env.ELEVENLABS_API_KEY,
    voiceId: 'custom-prasad-voice', // Pre-configured
    stabilityLevel: 0.75,
    similarityBoost: 0.85,
  },
  fallback: 'web-speech-api' // Browser built-in TTS
};

// Usage in backend
async function generateSpeech(text, voiceConfig = TTS_CONFIG) {
  try {
    const response = await googleCloudTTS.synthesizeSpeech({
      input: { text },
      voice: {
        languageCode: voiceConfig.googleCloud.languageCode,
        name: voiceConfig.googleCloud.voiceName,
      },
      audioConfig: {
        audioEncoding: voiceConfig.googleCloud.audioEncoding,
        speakingRate: voiceConfig.googleCloud.speakingRate,
        pitch: voiceConfig.googleCloud.pitch,
      },
    });
    return response.audioContent; // Base64 encoded MP3
  } catch (error) {
    console.error('TTS Error:', error);
    return null;
  }
}
```

---

## 🕷️ Job Scraping Implementation

### Supported Job Boards

```javascript
const JOB_SOURCES = {
  'LINKEDIN': {
    url: 'linkedin.com/jobs/search/',
    type: 'DYNAMIC', // Requires Puppeteer
    auth_required: true,
    difficulty: 'MEDIUM'
  },
  'GREENHOUSE': {
    url: 'api.greenhouse.io/v4/jobs',
    type: 'API',
    auth_required: false,
    difficulty: 'EASY',
    rate_limit: '100/min'
  },
  'WORKDAY': {
    url: 'jobs.workday.com',
    type: 'DYNAMIC',
    auth_required: false,
    difficulty: 'HARD',
    uses_ajax: true
  },
  'LEVER': {
    url: 'api.lever.co/v0/postings',
    type: 'API',
    auth_required: false,
    difficulty: 'EASY'
  },
  'ASHBY': {
    url: 'api.ashbyhq.com',
    type: 'API',
    auth_required: false,
    difficulty: 'EASY'
  },
  'INDEED': {
    url: 'indeed.com/jobs',
    type: 'DYNAMIC',
    auth_required: false,
    difficulty: 'MEDIUM'
  },
  'DICE': {
    url: 'dice.com/jobs',
    type: 'API + DYNAMIC',
    auth_required: false,
    difficulty: 'MEDIUM'
  },
  'ZIPRECRUITER': {
    url: 'ziprecruiter.com/jobs',
    type: 'API',
    auth_required: false,
    difficulty: 'EASY'
  },
  'COMPANY_CAREER': {
    url: 'company.com/careers', // (FAANG + others)
    type: 'DYNAMIC',
    auth_required: false,
    difficulty: 'VARIABLE',
    companies: [
      'google.com/careers',
      'careers.meta.com',
      'amazon.jobs',
      'careers.apple.com',
      'careers.microsoft.com',
      'jobs.netflix.com',
      'tesla.com/careers',
      // ... add more
    ]
  }
};
```

### Scraping Architecture
```javascript
// scraper-service.ts
import { Browser, Page } from 'puppeteer';
import axios from 'axios';

class JobScraperService {
  private browser: Browser;
  private proxies: string[] = []; // Rotating proxy list
  
  async initBrowser() {
    this.browser = await puppeteer.launch({
      headless: 'new',
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--single-process', // For resource-constrained environments
      ]
    });
  }

  async scrapeLinkedIn(searchParams: SearchParams) {
    const page = await this.browser.newPage();
    try {
      // LinkedIn login (use stored session/cookies)
      await page.goto('https://linkedin.com/jobs/search/', { waitUntil: 'networkidle2' });
      
      // Apply filters
      await page.evaluate((params) => {
        // Set location, keywords, salary range
        // Click search
      }, searchParams);
      
      // Wait for results to load
      await page.waitForSelector('[data-job-id]');
      
      // Scroll & extract all jobs
      const jobs = await page.evaluate(() => {
        return Array.from(document.querySelectorAll('[data-job-id]')).map(el => ({
          job_id: el.getAttribute('data-job-id'),
          title: el.querySelector('[data-job-name]')?.textContent,
          company: el.querySelector('[data-company-name]')?.textContent,
          location: el.querySelector('[data-location]')?.textContent,
          // ... extract more fields
        }));
      });
      
      return jobs;
    } finally {
      await page.close();
    }
  }

  async scrapeGreenhouse(companyName: string) {
    try {
      const response = await axios.get(
        `https://api.greenhouse.io/v4/jobs?organization=${companyName}`,
        { headers: { 'Authorization': `Bearer ${process.env.GREENHOUSE_API_KEY}` } }
      );
      return response.data.jobs;
    } catch (error) {
      console.error('Greenhouse API error:', error);
      return [];
    }
  }

  async scrapeWorkday(companyName: string, location: string) {
    const page = await this.browser.newPage();
    try {
      const url = `https://jobs.workday.com/${companyName}/jobs`;
      await page.goto(url, { waitUntil: 'networkidle2' });
      
      // Workday uses React - wait for content to render
      await page.waitForSelector('[data-automation="jobTitle"]', { timeout: 10000 });
      
      const jobs = await page.evaluate(() => {
        return Array.from(document.querySelectorAll('[data-automation="jobTitle"]')).map(el => ({
          title: el.textContent,
          // Extract other details...
        }));
      });
      
      return jobs;
    } finally {
      await page.close();
    }
  }

  async scrapeCompanyCareerPages(companies: string[]) {
    const allJobs = [];
    for (const companyUrl of companies) {
      const page = await this.browser.newPage();
      try {
        await page.goto(`https://${companyUrl}/careers`, { waitUntil: 'networkidle2' });
        // Company-specific selectors (might need manual configuration)
        const jobs = await page.evaluate(() => {
          // Generic selectors that work for most career pages
          return Array.from(document.querySelectorAll('[class*="job"], [data-job]')).map(el => ({
            title: el.querySelector('[class*="title"]')?.textContent,
            location: el.querySelector('[class*="location"]')?.textContent,
            description: el.querySelector('[class*="description"]')?.textContent,
            url: el.href,
          }));
        });
        allJobs.push(...jobs);
      } catch (error) {
        console.warn(`Failed to scrape ${companyUrl}:`, error.message);
      } finally {
        await page.close();
      }
    }
    return allJobs;
  }

  async scheduleRecurringScrapers() {
    // Run every 6 hours for active sources
    const cron = require('node-cron');
    
    cron.schedule('0 */6 * * *', async () => {
      console.log('Starting scheduled job scraping...');
      
      const results = {
        linkedin: await this.scrapeLinkedIn({ keyword: 'data engineer', location: 'United States' }),
        greenhouse: await this.scrapeGreenhouse('uber'),
        workday: await this.scrapeWorkday('netflix', 'United States'),
        // ... other sources
      };
      
      // Store in database
      await db.jobs.insertMany(results.flat());
    });
  }
}
```

---

## 💾 Session Logging & Persistence

### Real-time Log Streaming Architecture

```javascript
// backend/services/logger.service.ts
import { WebSocket } from 'ws';
import { createClient } from 'redis';

class SessionLoggerService {
  private redisClient = createClient();
  private wsConnections = new Map();

  async initializeSession(userId: string, sessionId: string) {
    // Store session metadata in Redis (volatile, 24hr expiry)
    await this.redisClient.setEx(
      `session:${sessionId}`,
      86400,
      JSON.stringify({
        userId,
        startedAt: new Date(),
        logsCount: 0,
      })
    );
  }

  async logEvent(userId: string, sessionId: string, logData: any) {
    const logEntry = {
      id: generateUUID(),
      user_id: userId,
      session_id: sessionId,
      log_type: logData.type, // 'VOICE_INPUT', 'JOB_APPLIED', etc.
      log_level: logData.level || 'INFO',
      message: logData.message,
      metadata: logData.metadata || {},
      timestamp: new Date(),
    };

    // 1. Store in persistent database (PostgreSQL)
    await db.sessionLogs.create(logEntry);

    // 2. Store in Redis cache for quick access (30-day window)
    const cacheKey = `logs:${sessionId}`;
    await this.redisClient.lpush(cacheKey, JSON.stringify(logEntry));
    await this.redisClient.expire(cacheKey, 2592000); // 30 days

    // 3. Stream to connected WebSocket clients (real-time)
    this.broadcastToSession(sessionId, logEntry);
  }

  broadcastToSession(sessionId: string, logEntry: any) {
    // Find all WebSocket connections for this session
    const connections = this.wsConnections.get(sessionId) || [];
    connections.forEach(ws => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: 'log_event',
          data: logEntry,
        }));
      }
    });
  }

  async retrieveSessionLogs(sessionId: string, limit: number = 500) {
    // Try Redis cache first
    const cachedLogs = await this.redisClient.lrange(`logs:${sessionId}`, 0, limit - 1);
    
    if (cachedLogs.length > 0) {
      return cachedLogs.map(log => JSON.parse(log));
    }

    // Fall back to database if cache is empty
    return await db.sessionLogs.findMany({
      where: { session_id: sessionId },
      take: limit,
      orderBy: { created_at: 'desc' },
    });
  }

  async getSessionAnalytics(userId: string, sessionId: string) {
    const logs = await db.sessionLogs.findMany({
      where: { user_id: userId, session_id: sessionId }
    });

    return {
      totalLogs: logs.length,
      voiceInteractions: logs.filter(l => l.log_type.includes('VOICE')).length,
      jobsApplied: logs.filter(l => l.log_type === 'JOB_APPLIED').length,
      jobsScraped: logs.filter(l => l.log_type === 'JOB_SCRAPED').length,
      emailsSent: logs.filter(l => l.log_type === 'EMAIL_SENT').length,
      errors: logs.filter(l => l.log_level === 'ERROR').length,
      sessionDuration: logs.length > 0 
        ? new Date(logs[logs.length - 1].timestamp) - new Date(logs[0].timestamp)
        : 0,
    };
  }

  // WebSocket connection handler
  registerWSClient(sessionId: string, ws: WebSocket) {
    if (!this.wsConnections.has(sessionId)) {
      this.wsConnections.set(sessionId, []);
    }
    this.wsConnections.get(sessionId).push(ws);

    // Send recent logs (last 20) on connect
    this.retrieveSessionLogs(sessionId, 20).then(logs => {
      ws.send(JSON.stringify({
        type: 'initial_logs',
        data: logs,
      }));
    });

    ws.on('close', () => {
      const connections = this.wsConnections.get(sessionId);
      const index = connections.indexOf(ws);
      if (index > -1) {
        connections.splice(index, 1);
      }
    });
  }
}
```

---

## 📧 Recruiter Follow-up Automation

### Email Follow-up Strategy

```javascript
// backend/services/recruiter-engagement.service.ts

class RecruiterEngagementService {
  async scheduleFollowUps(userId: string) {
    // Cron job: Check every 6 hours
    const recruiters = await db.recruiters.findMany({
      where: {
        user_id: userId,
        next_followup_date: { lte: new Date() }
      }
    });

    for (const recruiter of recruiters) {
      await this.sendFollowUpEmail(userId, recruiter);
    }
  }

  async sendFollowUpEmail(userId: string, recruiter: any) {
    // 1. Get user's email templates
    const templates = await db.emailTemplates.findMany({
      where: { user_id: userId }
    });

    // 2. Determine follow-up stage
    const dayssinceContact = Math.floor(
      (Date.now() - recruiter.last_contact_date) / (1000 * 60 * 60 * 24)
    );

    let templateType = 'INITIAL_FOLLOW_UP';
    if (dayssinceContact > 7) templateType = 'SECOND_FOLLOW_UP';
    if (dayssinceContact > 14) templateType = 'THIRD_FOLLOW_UP';

    const template = templates.find(t => t.template_type === templateType);

    // 3. Compile email using template variables
    const emailContent = this.compileTemplate(template, {
      recruiter_name: recruiter.recruiter_name,
      company_name: recruiter.company_name,
      job_title: recruiter.job_title,
      days_since_contact: dayssinceContact,
    });

    // 4. Send via Gmail API
    const mailOptions = {
      to: recruiter.recruiter_email,
      subject: emailContent.subject,
      html: emailContent.body,
      replyTo: recruiter.recruiter_email,
    };

    const messageId = await this.sendViaGmail(userId, mailOptions);

    // 5. Log email in database
    await db.sentEmails.create({
      user_id: userId,
      recruiter_id: recruiter.id,
      recipient_email: recruiter.recruiter_email,
      subject: emailContent.subject,
      body: emailContent.body,
      sent_at: new Date(),
      sent_via: 'GMAIL_API',
      messageId,
    });

    // 6. Schedule next follow-up (7 days later)
    await db.recruiters.update({
      where: { id: recruiter.id },
      data: {
        last_contact_date: new Date(),
        contact_count: recruiter.contact_count + 1,
        next_followup_date: new Date(Date.now() + 7 * 24 * 60 * 60 * 1000), // 7 days
      }
    });
  }

  compileTemplate(template: any, variables: any): { subject: string; body: string } {
    let subject = template.subject_template;
    let body = template.body_template;

    // Replace variables
    for (const [key, value] of Object.entries(variables)) {
      const regex = new RegExp(`{{${key}}}`, 'g');
      subject = subject.replace(regex, String(value));
      body = body.replace(regex, String(value));
    }

    return { subject, body };
  }

  // Extract recruiter from job posting
  async extractRecruiterFromJob(jobId: string, userId: string) {
    const job = await db.jobs.findUnique({ where: { id: jobId } });
    
    // Parse job description for email patterns
    const emailRegex = /([a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+\.[a-zA-Z0-9_-]+)/gi;
    const phoneRegex = /(\+?1)?\s?\(?(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})/g;
    
    const emails = job.job_description.match(emailRegex) || [];
    const phones = job.job_description.match(phoneRegex) || [];

    // Extract recruiter name (often in signature or contact line)
    const nameMatch = job.job_description.match(/(?:Contact|Reach|Email).*?:\s*([A-Za-z\s]+)\s*(?:at|@|\n)/i);
    const recruiterName = nameMatch ? nameMatch[1] : 'Recruiter';

    // Create recruiter entry if not exists
    if (emails.length > 0) {
      const existingRecruiter = await db.recruiters.findFirst({
        where: {
          user_id: userId,
          recruiter_email: emails[0],
          company_name: job.company_name,
        }
      });

      if (!existingRecruiter) {
        await db.recruiters.create({
          user_id: userId,
          recruiter_name: recruiterName,
          recruiter_email: emails[0],
          recruiter_phone: phones[0],
          company_name: job.company_name,
          job_id: jobId,
          source: 'EXTRACTED_FROM_JOB',
          first_contact_date: new Date(),
          next_followup_date: new Date(Date.now() + 7 * 24 * 60 * 60 * 1000),
        });
      }
    }
  }
}
```

---

## 🚀 Auto-Apply Implementation

### Form Filling Strategy

```javascript
// backend/services/auto-apply.service.ts

class AutoApplyService {
  async autoApplyToJob(userId: string, jobId: string) {
    const user = await db.users.findUnique({ where: { id: userId } });
    const job = await db.jobs.findUnique({ where: { id: jobId } });

    // 1. Determine job source/platform
    const applicationType = this.determineApplicationType(job.source, job.job_url);

    let applicationResult;

    switch (applicationType) {
      case 'GREENHOUSE_API':
        applicationResult = await this.applyViaGreenhouse(user, job);
        break;
      case 'LEVER_API':
        applicationResult = await this.applyViaLever(user, job);
        break;
      case 'LINKEDIN_DIRECT':
        applicationResult = await this.applyViaLinkedIn(user, job);
        break;
      case 'WORKDAY_FORM':
        applicationResult = await this.applyViaWorkday(user, job);
        break;
      case 'GENERIC_FORM':
        applicationResult = await this.applyViaGenericForm(user, job);
        break;
      default:
        applicationResult = { success: false, error: 'Unknown job source' };
    }

    // 2. Log application
    const application = await db.applications.create({
      user_id: userId,
      job_id: jobId,
      application_status: applicationResult.success ? 'APPLIED' : 'DRAFT',
      applied_at: new Date(),
      applied_method: 'AUTO_APPLY',
      auto_apply_success: applicationResult.success,
      auto_apply_error: applicationResult.error,
      form_data_submitted: applicationResult.formData,
      submission_confirmation: applicationResult.confirmation,
    });

    return application;
  }

  async applyViaGreenhouse(user: any, job: any) {
    try {
      // 1. Extract Greenhouse job ID from URL
      const jobId = new URL(job.job_url).searchParams.get('gh_jid');
      
      // 2. Prepare application data
      const formData = {
        first_name: user.full_name.split(' ')[0],
        last_name: user.full_name.split(' ')[1] || '',
        email: user.email,
        phone_number: user.phone,
        location: user.location,
        linkedin_profile_url: user.linkedin_url,
        portfolio_url: 'https://shyamkarri.github.io',
        resume: await this.fetchResumeFile(user.resume_url),
        cover_letter: await this.generateCoverLetter(user, job),
      };

      // 3. Submit via Greenhouse API
      const response = await axios.post(
        `https://api.greenhouse.io/v4/applications`,
        formData,
        {
          headers: {
            'Authorization': `Bearer ${process.env.GREENHOUSE_API_KEY}`,
            'Content-Type': 'multipart/form-data',
          }
        }
      );

      return {
        success: true,
        formData,
        confirmation: response.data.application_id,
      };
    } catch (error) {
      return {
        success: false,
        error: error.message,
      };
    }
  }

  async applyViaLinkedIn(user: any, job: any) {
    // Use Puppeteer to automate LinkedIn apply
    const browser = await puppeteer.launch();
    const page = await browser.newPage();

    try {
      // Login with session cookies
      await page.goto(job.job_url, { waitUntil: 'networkidle2' });
      
      // Click "Easy Apply" button
      await page.click('[aria-label="Easy Apply"]');
      await page.waitForTimeout(1000);

      // Fill form fields
      const formFields = await page.$$('.artdeco-inline-feedback-form input, .artdeco-inline-feedback-form textarea');
      
      for (const field of formFields) {
        const placeholder = await field.evaluate(el => el.placeholder);
        const value = this.mapFieldValue(placeholder, user);
        if (value) {
          await field.type(value);
        }
      }

      // Submit
      await page.click('[data-test-form-submit]');
      
      const confirmation = await page.evaluate(() => 
        document.querySelector('[data-test-success-message]')?.textContent
      );

      return {
        success: !!confirmation,
        formData: {},
        confirmation: confirmation || 'Applied Successfully',
      };
    } catch (error) {
      return {
        success: false,
        error: error.message,
      };
    } finally {
      await browser.close();
    }
  }

  async applyViaGenericForm(user: any, job: any) {
    // Fallback for unrecognized job posting formats
    const browser = await puppeteer.launch();
    const page = await browser.newPage();

    try {
      await page.goto(job.job_url);
      
      // Auto-detect form fields
      const formInputs = await page.$$('input[type="text"], input[type="email"], textarea');
      
      for (const input of formInputs) {
        const name = await input.evaluate(el => el.name || el.id || el.placeholder);
        const value = this.smartMapField(name, user);
        
        if (value) {
          await input.type(value);
        }
      }

      // Upload resume
      const fileInputs = await page.$$('input[type="file"]');
      if (fileInputs.length > 0) {
        const resumePath = await this.downloadResume(user.resume_url);
        await fileInputs[0].uploadFile(resumePath);
      }

      // Submit form
      const submitBtn = await page.$('button[type="submit"], input[type="submit"]');
      if (submitBtn) await submitBtn.click();

      const success = await page.waitForNavigation().then(() => true).catch(() => false);

      return {
        success,
        formData: { filled_fields: formInputs.length },
        confirmation: success ? 'Form submitted' : 'Unknown',
      };
    } catch (error) {
      return {
        success: false,
        error: error.message,
      };
    } finally {
      await browser.close();
    }
  }

  mapFieldValue(fieldName: string, user: any): string | null {
    const lowerFieldName = fieldName.toLowerCase();
    
    if (lowerFieldName.includes('first')) return user.full_name.split(' ')[0];
    if (lowerFieldName.includes('last')) return user.full_name.split(' ')[1];
    if (lowerFieldName.includes('email')) return user.email;
    if (lowerFieldName.includes('phone')) return user.phone;
    if (lowerFieldName.includes('location')) return user.location;
    if (lowerFieldName.includes('linkedin')) return user.linkedin_url;
    if (lowerFieldName.includes('github')) return user.github_url;
    
    return null;
  }

  async generateCoverLetter(user: any, job: any) {
    // Use Claude API to generate personalized cover letter
    const prompt = `
      Write a professional cover letter for this job application:
      
      Applicant: ${user.full_name}
      Background: ${user.years_experience} years as a ${user.preferred_roles}
      
      Job Title: ${job.job_title}
      Company: ${job.company_name}
      Job Description: ${job.job_description.substring(0, 500)}
      
      Keep it concise (200 words), professional, and tailored to the role.
    `;

    const response = await anthropic.messages.create({
      model: 'claude-opus-4-1',
      max_tokens: 500,
      messages: [{ role: 'user', content: prompt }]
    });

    return response.content[0].type === 'text' ? response.content[0].text : '';
  }
}
```

---

## 🎨 Enhanced Frontend - Next.js Portfolio

### Project Structure
```
frontend/
├── pages/
│   ├── index.tsx (Hero + Voice Assistant)
│   ├── dashboard.tsx (Job Tracking)
│   ├── applications.tsx (Application History)
│   ├── recruiters.tsx (Recruiter CRM)
│   ├── settings.tsx (Configuration)
│   └── auth/
│       ├── login.tsx
│       ├── register.tsx
│       └── verify.tsx
├── components/
│   ├── VoiceAssistant/ (Enhanced AI voice)
│   ├── JobCard/ (Job listing)
│   ├── Dashboard/ (Analytics)
│   ├── LogViewer/ (Real-time logs)
│   └── RecruitersPanel/ (CRM interface)
├── hooks/
│   ├── useAuth.ts
│   ├── useJobSearch.ts
│   ├── useWebSocket.ts (Real-time logs)
│   └── useVoiceAssistant.ts
├── styles/
│   ├── globals.css (Tailwind + Glassmorphism)
│   └── animations.css
└── utils/
    ├── api.ts
    ├── auth.ts
    └── websocket.ts
```

### Key Features

**1. Dashboard with Analytics**
```typescript
// components/Dashboard/JobAnalytics.tsx
export const JobAnalytics = ({ userId }: Props) => {
  const [stats, setStats] = useState(null);
  
  useEffect(() => {
    fetch(`/api/analytics/${userId}`).then(res => res.json()).then(setStats);
  }, [userId]);

  return (
    <div className="grid grid-cols-4 gap-4 p-6">
      <StatCard 
        title="Applications" 
        value={stats?.totalApplications} 
        trend="+23% this week"
      />
      <StatCard 
        title="Active Opportunities" 
        value={stats?.activeJobs} 
        trend="5 new today"
      />
      <StatCard 
        title="Recruiter Contacts" 
        value={stats?.recruiterCount} 
        trend="12 pending follow-ups"
      />
      <StatCard 
        title="Conversion Rate" 
        value={`${stats?.conversionRate}%`} 
        trend="Above industry avg"
      />
    </div>
  );
};
```

**2. Real-time Log Viewer with WebSocket**
```typescript
// components/LogViewer/SessionLogs.tsx
export const SessionLogs = ({ sessionId }: Props) => {
  const [logs, setLogs] = useState<Log[]>([]);
  const ws = useWebSocket(`wss://api.yoursite.com/logs/${sessionId}`);

  useEffect(() => {
    if (!ws) return;

    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      
      if (message.type === 'initial_logs') {
        setLogs(message.data);
      } else if (message.type === 'log_event') {
        setLogs(prev => [message.data, ...prev]);
      }
    };
  }, [ws]);

  return (
    <div className="bg-gray-900 rounded-lg p-4 max-h-96 overflow-y-auto font-mono text-sm">
      {logs.map((log) => (
        <div key={log.id} className={`py-1 text-${getLogColor(log.log_level)}`}>
          <span className="text-gray-500">[{log.timestamp}]</span>
          {' '}
          <span className="text-cyan-400">{log.log_type}</span>
          {' '}
          <span>{log.message}</span>
        </div>
      ))}
    </div>
  );
};
```

**3. Enhanced Voice Assistant**
```typescript
// components/VoiceAssistant/VoiceAssistant.tsx
export const VoiceAssistant = () => {
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [isListening, setIsListening] = useState(false);
  const [audioStream, setAudioStream] = useState<MediaStream | null>(null);
  
  const handleVoiceInput = async (transcript: string) => {
    // Send to backend
    const response = await fetch('/api/chat', {
      method: 'POST',
      body: JSON.stringify({ message: transcript, session_id: sessionId })
    });

    const { reply, audio_base64 } = await response.json();

    // Log interaction
    logEvent({
      type: 'VOICE_INPUT',
      message: transcript,
      session_id: sessionId
    });

    // Play audio response with South Indian accent
    if (audio_base64) {
      setIsSpeaking(true);
      const audio = new Audio(`data:audio/mp3;base64,${audio_base64}`);
      audio.onended = () => setIsSpeaking(false);
      audio.play();

      // Log output
      logEvent({
        type: 'VOICE_OUTPUT',
        message: reply,
        session_id: sessionId
      });
    }
  };

  return (
    <div className="voice-assistant-widget">
      <AnimatedOrb isListening={isListening} isSpeaking={isSpeaking} />
      <VoiceControlBar 
        onStart={startListening}
        onStop={stopListening}
        isActive={isListening}
      />
      <Transcript messages={messages} />
    </div>
  );
};
```

---

## 🔌 API Endpoints

### Authentication
```
POST   /api/auth/register         - Create account
POST   /api/auth/login            - Login with email/password
POST   /api/auth/verify-email     - Verify email with OTP
POST   /api/auth/refresh-token    - Refresh JWT
POST   /api/auth/logout           - Logout

POST   /api/auth/oauth/google     - Google OAuth login
POST   /api/auth/oauth/linkedin   - LinkedIn OAuth login
```

### Jobs
```
GET    /api/jobs?source=LINKEDIN&skills=Python,Java - Search jobs
GET    /api/jobs/{id}             - Get job details
POST   /api/jobs/manual-add       - Manually add job
POST   /api/jobs/scrape           - Trigger manual scrape
GET    /api/jobs/recommendations  - AI-recommended jobs
```

### Applications
```
GET    /api/applications          - Get all applications
GET    /api/applications/{id}     - Get application details
POST   /api/applications          - Create/apply for job
PATCH  /api/applications/{id}     - Update application status
DELETE /api/applications/{id}     - Withdraw application

POST   /api/applications/{id}/auto-apply - Trigger auto-apply
```

### Recruiters
```
GET    /api/recruiters            - Get all recruiter contacts
GET    /api/recruiters/{id}       - Get recruiter details
POST   /api/recruiters            - Add recruiter manually
PATCH  /api/recruiters/{id}       - Update recruiter info
POST   /api/recruiters/{id}/follow-up - Send manual follow-up

GET    /api/recruiters/analytics  - CRM analytics
```

### Logs & Sessions
```
GET    /api/logs/sessions         - List all sessions
GET    /api/logs/sessions/{id}    - Get session logs
GET    /api/logs/analytics/{sessionId} - Session analytics
WS     /api/logs/stream/{sessionId} - WebSocket stream
```

### Email Templates
```
GET    /api/templates             - Get all templates
POST   /api/templates             - Create template
PATCH  /api/templates/{id}        - Update template
DELETE /api/templates/{id}        - Delete template
```

### Chat/Voice
```
POST   /api/chat                  - Send message (text)
WS     /api/voice/stream          - Voice input stream
POST   /api/tts                   - Text-to-speech generation
```

---

## 🛠️ Technology Stack

### Backend
```
Framework: FastAPI (Python) or Node.js/Express
Database: PostgreSQL (primary) + Redis (cache/sessions)
Real-time: WebSockets + Socket.io
Job Scraping: Puppeteer/Playwright + rotating proxies
Email: Gmail API + Nodemailer
TTS: Google Cloud Speech-to-Text + ElevenLabs
LLM: Claude API (Anthropic) for chat/cover letter generation
Auth: JWT + OAuth2
```

### Frontend
```
Framework: Next.js 14 (React)
Styling: Tailwind CSS + CSS-in-JS animations
State: React Query (TanStack Query) + Zustand
Voice: Web Speech API + getUserMedia
Charts: Recharts / Chart.js
Real-time: WebSocket client
```

### Infrastructure
```
Deployment: Docker + Docker Compose
Hosting: AWS EC2 / Railway.app / Render.com
Database Hosting: AWS RDS / Railway
CDN: CloudFlare (optional)
Email: SendGrid / Mailgun (fallback)
```

---

## 📋 Implementation Checklist

### Phase 1: Backend Infrastructure (Week 1-2)
- [ ] Set up PostgreSQL with schema
- [ ] Implement JWT authentication
- [ ] Create session management with Redis
- [ ] Build WebSocket server for log streaming
- [ ] Create logger service with persistence
- [ ] Set up job scraping service
- [ ] Implement Greenhouse/Lever API integrations

### Phase 2: Job Management (Week 2-3)
- [ ] Complete LinkedIn scraper (Puppeteer)
- [ ] Complete Workday scraper
- [ ] Add company career page scrapers (Google, Meta, Amazon, Microsoft, Apple, Netflix, Tesla)
- [ ] Implement job filtering & matching
- [ ] Create job API endpoints
- [ ] Schedule automatic job scraping (cron jobs)

### Phase 3: Auto-Apply System (Week 3-4)
- [ ] Implement generic form filler
- [ ] Implement Greenhouse API apply
- [ ] Implement Lever API apply
- [ ] Implement LinkedIn easy-apply automation
- [ ] Cover letter generation via Claude API
- [ ] Resume upload & file management
- [ ] Track application success rates

### Phase 4: Recruiter CRM (Week 4-5)
- [ ] Recruiter extraction from job postings
- [ ] Email follow-up scheduling
- [ ] Gmail API integration
- [ ] Email template system
- [ ] Follow-up analytics
- [ ] Recruiter relationship scoring

### Phase 5: Enhanced AI Voice Assistant (Week 5-6)
- [ ] Integrate Google Cloud TTS (en-IN voice)
- [ ] Fix grammar/speech quality
- [ ] Add conversation context memory
- [ ] Implement ElevenLabs fallback
- [ ] Create South Indian English prompt tuning
- [ ] Test voice clarity & accent

### Phase 6: Frontend Portal (Week 6-7)
- [ ] Redesign hero section with enhanced portfolio
- [ ] Build job dashboard with analytics
- [ ] Create applications tracker
- [ ] Build recruiter CRM interface
- [ ] Implement real-time log viewer
- [ ] Add session analytics
- [ ] Settings/configuration page

### Phase 7: Security & Authentication (Week 7-8)
- [ ] Implement OAuth2 (Google, LinkedIn, GitHub)
- [ ] Add email verification
- [ ] Password reset flow
- [ ] Encrypt sensitive data
- [ ] Add rate limiting
- [ ] Implement CORS properly
- [ ] Security audit

### Phase 8: Testing & Deployment (Week 8)
- [ ] Write unit tests
- [ ] Integration tests for APIs
- [ ] End-to-end tests
- [ ] Performance optimization
- [ ] Docker containerization
- [ ] Deploy to production
- [ ] Set up monitoring & logging

---

## 🚀 Quick Start Commands

```bash
# Clone & Setup
git clone <your-repo-url>
cd job-automation-platform

# Install dependencies
npm install && pip install -r requirements.txt

# Environment setup
cp .env.example .env
# Fill in your credentials:
# - DATABASE_URL=postgres://...
# - GOOGLE_CLOUD_API_KEY=...
# - ANTHROPIC_API_KEY=...
# - ELEVENLABS_API_KEY=...
# - GREENHOUSE_API_KEY=...

# Database migration
npm run migrate

# Start development
npm run dev              # Frontend (port 3000)
python -m uvicorn app.main:app --reload  # Backend (port 8000)

# Start scraper service
npm run scraper

# Run tests
npm run test

# Deploy
npm run build
docker build -t job-automation .
docker run -p 8000:8000 job-automation
```

---

## 📞 Support & Next Steps

1. **Run this document through Fable 5** with:
   ```
   "Use this requirements doc to build a complete job automation platform with:
   - PostgreSQL + Redis backend
   - Next.js frontend with Tailwind CSS
   - Real-time log streaming via WebSocket
   - AI voice assistant with South Indian English accent
   - Job scraping from 50+ boards
   - Auto-apply + recruiter CRM
   - Full authentication & security"
   ```

2. **Provide additional context when running Fable 5:**
   - Your current backend URL
   - Existing database schema
   - API keys you already have
   - Preferred deployment platform

3. **Critical API Keys Needed:**
   - Google Cloud TTS API key
   - Anthropic API key (Claude)
   - ElevenLabs API key (optional)
   - Greenhouse/Lever API keys
   - Gmail API credentials
   - LinkedIn/GitHub OAuth credentials

---

**Created:** 2026-07-04
**Last Updated:** Now
**Status:** Ready for Fable 5 implementation
