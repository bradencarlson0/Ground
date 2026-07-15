# Huffman DFW Medical Office Parcel Refresh

Automated live refresh and parcel-grain spatial join for the 460 current Frisco, Prosper, Celina, and McKinney candidates. The workflow pulls county CAD parcel polygons and values, municipal zoning, Microsoft GlobalML building footprints, FEMA flood hazard, NWI wetlands, PUCT water/sewer CCN service areas and facility lines, TCEQ districts, hospital locations, and TxDOT roadway/AADT sources when discoverable. It generates GeoPackage, GeoParquet, GeoJSON, ranked CSV, source-run logs, and QA reports.
