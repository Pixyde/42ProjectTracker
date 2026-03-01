import os
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from tqdm import tqdm

class IntraScrape:
	SELECTORS = {
		'projects_list': 'ul.projects-list--list',
		'project_item': 'li.project-item',
		'project_name': 'div.project-name',
		'attachment_name': 'h4.attachment-name'
	}
	
	def __init__(self, cookies):
		self.session = requests.Session()
		self.session.cookies.update(cookies)
		self.all_projects = []
		self.projects_to_scrape = []
		self.base_url = "https://projects.intra.42.fr"
	
	def _get_project_list_page(self, page: int = 1):
		"""
		Helper method to retrieve the raw list of project items from a single page.

		Args:
			page (int): Page number to retrieve (default: 1)

		Returns:
			list[BeautifulSoup Tag]: List of project item HTML elements
		"""
		url = f"{self.base_url}/projects/list?page={page}"
		response = self.session.get(url)
		if response.status_code != 200:
			raise Exception(f"Failed to retrieve projects: {response.status_code}")
		soup = BeautifulSoup(response.text, 'html.parser')
		# Use select_one() to find the first matching element
		projects_ul = soup.select_one(self.SELECTORS['projects_list'])
		if not projects_ul:
			return []
		# Use select() to find all matching elements
		project_items = projects_ul.select(self.SELECTORS['project_item'])
		return project_items

	def _get_total_pages(self):
		"""
		Get the total number of pages by checking the pagination on the first page.

		Returns:
			int: Total number of pages
		"""
		url = f"{self.base_url}/projects/list"
		response = self.session.get(url)
		if response.status_code != 200:
			raise Exception(f"Failed to retrieve projects: {response.status_code}")
		soup = BeautifulSoup(response.text, 'html.parser')

		# Find last page link: #projects-list-container > div > ul > li.last > a
		last_page_link = soup.select_one('#projects-list-container > div > ul > li.last > a')
		if last_page_link and last_page_link.get('href'):
			# Extract page number from href like "/projects/list?page=17"
			href = last_page_link['href']
			if 'page=' in href:
				return int(href.split('page=')[1].split('&')[0])

		# Fallback: assume only 1 page
		return 1

	def _get_project_list(self, parallel: bool = True, max_workers: int = None):
		"""
		Helper method to retrieve all project items from all pages.

		Args:
			parallel (bool): Whether to fetch pages in parallel (default: True)
			max_workers (int): Number of parallel threads. If None, uses min(32, cpu_count * 2).
			                   Only used when parallel=True (default: None)

		Returns:
			list[BeautifulSoup Tag]: List of all project item HTML elements
		"""
		total_pages = self._get_total_pages()

		if not parallel:
			# Sequential fetching
			print(f"Fetching {total_pages} pages sequentially...")
			all_items = []
			for page in range(1, total_pages + 1):
				items = self._get_project_list_page(page)
				all_items.extend(items)
				print(f"Loaded page {page}/{total_pages}: {len(items)} projects")
			print(f"Total projects loaded: {len(all_items)}")
			return all_items

		# Parallel fetching
		if max_workers is None:
			# For I/O-bound tasks (web scraping), use 2x CPU count, capped at 32
			max_workers = min(32, (os.cpu_count() or 4) * 2)

		print(f"Fetching {total_pages} pages in parallel with {max_workers} workers...")

		executor = ThreadPoolExecutor(max_workers=max_workers)
		try:
			results = list(executor.map(self._get_project_list_page, range(1, total_pages + 1)))
		except KeyboardInterrupt:
			print("\n\n⚠️  Interrupt received during page fetching. Cleaning up...")
			executor.shutdown(wait=False, cancel_futures=True)
			raise
		finally:
			executor.shutdown(wait=True)

		# Flatten results
		all_items = []
		for items in results:
			all_items.extend(items)

		print(f"Total projects loaded: {len(all_items)}")
		return all_items

	def get_projects_to_scrape(self, project_names: list[str], parallel: bool = True, max_workers: int = None) -> list[dict]:
		"""
		Filters and retrieves the list of projects to scrape based on provided names.
		Args:
			project_names (list[str]): List of project names to scrape
			parallel (bool): Whether to fetch pages in parallel (default: True)
			max_workers (int): Number of parallel threads (default: auto-detect)
		Returns:
			list[dict]: List of projects with 'name' and 'url' keys
		"""
		if not self.all_projects:
			self.get_all_projects(parallel=parallel, max_workers=max_workers)
		filtered_projects = [
			proj for proj in self.all_projects
			if proj['name'].lower() in (name.lower() for name in project_names)
		]
		self.projects_to_scrape = filtered_projects
		return filtered_projects

	def get_all_projects(self, parallel: bool = True, max_workers: int = None) -> list[dict]:
		"""
		Retrieves the list of projects from the 42 intra projects page.
		Args:
			parallel (bool): Whether to fetch pages in parallel (default: True)
			max_workers (int): Number of parallel threads (default: auto-detect)
		Returns:
			list[dict]: List of projects with 'name' and 'url' keys
		"""
		all_projects = []
		project_items = self._get_project_list(parallel=parallel, max_workers=max_workers)
		for item in project_items:
			# Use select_one() with the selector from SELECTORS
			project_name_div = item.select_one(self.SELECTORS['project_name'])
			if not project_name_div:
				continue
			project_link = project_name_div.find('a')
			if not project_link:
				continue
			all_projects.append({
				'name': project_link.text.strip(),
				'url': project_link['href']
			})
		self.all_projects = all_projects
		return all_projects
	
	def get_project_attachments(self, project_url: str) -> list[str]:
		"""
		Retrieves the list of attachment links for a given project.
		Args:
			project_url (str): URL path of the project
		Returns:
			list[str]: List of attachment URLs
		"""
		url = self.base_url + project_url
		response = self.session.get(url)
		if response.status_code != 200:
			raise Exception(f"Failed to retrieve project page: {response.status_code}")
		soup = BeautifulSoup(response.text, 'html.parser')
		# Use select() to find all matching elements
		attachments_div = soup.select(self.SELECTORS['attachment_name'])
		if not attachments_div:
			tqdm.write(f"No attachments found for project: {project_url}")
			return []
		links = [att.find('a')['href'] for att in attachments_div if att.find('a')]
		return links
	
	def get_project_url_by_name(self, project_name: str) -> str:
		"""
		Retrieves the URL of a project given its name.
		Args:
			project_name (str): Name of the project to search for
		Returns:
			str: URL of the project or None if not found
		"""
		if not self.all_projects:
			self.get_all_projects()
		url = [proj['url'] for proj in self.all_projects if proj['name'].lower() == project_name.lower()]
		return url[0] if url else None

	def get_remote_modified_time(self, attachment_url: str) -> float:
		"""
		Get the last modified timestamp of a remote file without downloading it.

		Args:
			attachment_url (str): URL of the attachment

		Returns:
			float: Unix timestamp of last modification, or 0 if not available
		"""
		try:
			response = self.session.head(attachment_url, timeout=10)
			if response.status_code == 200:
				last_modified = response.headers.get('Last-Modified')
				if last_modified:
					dt = parsedate_to_datetime(last_modified)
					return dt.timestamp()
		except Exception:
			pass
		return 0

	def get_versioned_filepath(self, attachment_url: str, base_save_path: str) -> tuple[str, bool]:
		"""
		Determine the filepath for saving, using remote timestamp in filename.

		Strategy:
		- If no file exists, use base filename
		- If base file exists and matches remote timestamp, skip download
		- If file exists with different timestamp, create versioned filename
		- Returns the path to use and whether a download should occur

		Args:
			attachment_url (str): URL of the remote attachment
			base_save_path (str): Original save path (e.g., "/path/to/file.pdf")

		Returns:
			tuple[str, bool]: (filepath to save to, whether to download)
		"""
		remote_time = self.get_remote_modified_time(attachment_url)
		if remote_time == 0:
			# Can't determine remote time, don't download
			return (base_save_path, False)

		# Parse base path
		directory = os.path.dirname(base_save_path) or '.'
		filename = os.path.basename(base_save_path)
		base_name, ext = os.path.splitext(filename)

		# Format remote timestamp
		timestamp_str = datetime.fromtimestamp(remote_time).strftime('%Y%m%d_%H%M%S')
		versioned_filename = f"{base_name}_{timestamp_str}{ext}"
		versioned_path = os.path.join(directory, versioned_filename)

		# Check if versioned file already exists
		if os.path.exists(versioned_path):
			return (versioned_path, False)

		# Check if base file exists
		if os.path.exists(base_save_path):
			# Check if base file matches remote timestamp (tolerance of 1 second)
			local_time = os.path.getmtime(base_save_path)
			if abs(local_time - remote_time) <= 1:
				# Base file is same version as remote
				return (base_save_path, False)
			# Base file is different version, create versioned file
			return (versioned_path, True)

		# No files exist yet, use base filename for first download
		return (base_save_path, True)

	def download_attachment(self, attachment_url: str, save_path: str, show_progress: bool = True):
		"""
		Download an attachment with optional progress bar and set modification time

		Args:
			attachment_url (str): URL of the attachment to download
			save_path (str): Path where to save the file
			show_progress (bool): Whether to show a progress bar for large files (default: True)
		"""
		with self.session.get(attachment_url, stream=True, timeout=30) as response:
			if response.status_code != 200:
				raise Exception(f"Failed to download attachment: {response.status_code}")

			# Get total file size and last modified time from headers
			total_size = int(response.headers.get('content-length', 0))
			last_modified = response.headers.get('Last-Modified')

			# Only show progress bar for files larger than 1MB
			if show_progress and total_size > 1_000_000:
				# Extract filename for progress bar description
				filename = save_path.split('/')[-1]
				progress_bar = tqdm(
					total=total_size,
					unit='B',
					unit_scale=True,
					unit_divisor=1024,
					desc=f"    {filename}",
					leave=False
				)
			else:
				progress_bar = None

			with open(save_path, 'wb') as f:
				for chunk in response.iter_content(chunk_size=8192):
					if chunk:
						f.write(chunk)
						if progress_bar:
							progress_bar.update(len(chunk))

			if progress_bar:
				progress_bar.close()

		# Set the file's modification time to match the remote file
		if last_modified:
			try:
				dt = parsedate_to_datetime(last_modified)
				timestamp = dt.timestamp()
				os.utime(save_path, (timestamp, timestamp))
			except Exception:
				pass

class IntraAPI:
	def __init__(self, uid, secret):
		self.uid = uid
		self.secret = secret
		self.url = "https://api.intra.42.fr"
		self.headers = {
			'Content-Type': 'application/x-www-form-urlencoded'
		}
		self.token = None
		self.get_token()
	
	def get(self, endpoint, params=None, page=None, page_size=30):
		"""
		Get request to the 42 API with optional pagination

		Args:
			endpoint (str): API endpoint
			params (dict, optional): Query parameters. Defaults to {}.
			page (int, optional): Page number for pagination. Defaults to None.
			page_size (int, optional): Number of items per page. Defaults to 30.
		"""
		if params is None:
			params = {}
		params = params.copy()
		if page is not None:
			params['page[number]'] = page
			params['page[size]'] = page_size
		if self.token:
			self.headers['Authorization'] = 'Bearer ' + self.token
		response = requests.get(
			self.url + endpoint, 
			headers=self.headers, 
			params=params
		)
		response.raise_for_status()
		try:
			return response.json()
		except requests.JSONDecodeError:
			raise Exception(f"Invalid JSON response from {endpoint}")

	def get_paginated(self, endpoint, params=None, page_size=100):
		"""
		Generator to get all items from a paginated endpoint

		Args:
			endpoint (str): API endpoint
			params (dict, optional): Query parameters. Defaults to {}.
			page_size (int, optional): Number of items per page. Defaults to 100.
		Yields:
			dict: Individual items from the paginated response
		"""
		if params is None:
			params = {}
		params = params.copy()
		page = 1
		while True:
			params['page[number]'] = page
			params['page[size]'] = page_size
			results = self.get(endpoint, params=params)
			if not results:
				break
			for item in results:
				yield item
			if len(results) < page_size:
				break
			page += 1
	
	def get_all_pages(self, endpoint, params={}, page_size=100):
		return list(self.get_paginated(endpoint, params=params, page_size=page_size))

	def post(self, endpoint, payload={}):
		if self.token:
			self.headers['Authorization'] = 'Bearer ' + self.token
		response = requests.post(
			self.url + endpoint, 
			headers=self.headers, 
			data=payload
		)
		return response.json()

	def get_token(self):
		oauth_response = self.post("/oauth/token", {"grant_type": "client_credentials", \
                            "client_id": self.uid, "client_secret": self.secret})
		if not oauth_response.get("access_token"):
			raise Exception(oauth_response.get("error_description"))
		self.token = oauth_response["access_token"]
